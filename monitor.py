#!/usr/bin/env python3
"""Colorado DMV driver-license appointment slot monitor.

Polls the Colorado DMV online appointment system (Q-Flow at
coloradoappt.cxmflow.com) for a set of offices, looks for "Renew"
appointment dates within a near-term window (default 1-7 days from
today), and sends a push notification via ntfy when a new early slot
appears.

Only the read-only steps of the booking wizard are used
(office -> service -> date list). No appointment is ever created.
"""

import html
import json
import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuration (override any of these with environment variables)
# --------------------------------------------------------------------------

BASE_URL = os.environ.get("DMV_BASE_URL", "https://coloradoappt.cxmflow.com")
# Entry GUID for the appointment wizard (office -> service -> date/time).
START_PATH = os.environ.get(
    "DMV_START_PATH",
    "/Appointment/Index/d74f48b1-33a9-428c-acd1-d7d1bfc9555c",
)
BOOKING_URL = BASE_URL + START_PATH

# Known Colorado driver-license office ids (ParentUnitId) near Denver.
OFFICE_NAMES = {
    44: "Westminster",
    81: "Adams (Westminster/Pecos)",
    12: "Denver NE",
    91: "Denver Regional Service Center",
    10: "Aurora",
    14: "Centennial",
    29: "Loveland",
    85: "Boulder",
    92: "Longmont",
    13: "Golden",
    20: "Parker",
    24: "Fort Collins",
    27: "Greeley",
}

OFFICE_IDS = [
    int(x)
    for x in os.environ.get("OFFICE_IDS", "44,12,10,14,29").split(",")
    if x.strip()
]

# Service to watch. The service list is re-read per office and matched by
# name; SERVICE_ID is the fallback if the name match finds nothing.
SERVICE_MATCH = os.environ.get("SERVICE_MATCH", "renew colorado driver license").lower()
SERVICE_ID_FALLBACK = int(os.environ.get("SERVICE_ID", "1322"))

# Alert window: slot date must be >= today+MIN_DAYS and <= today+MAX_DAYS.
MIN_DAYS = int(os.environ.get("MIN_DAYS", "1"))
MAX_DAYS = int(os.environ.get("MAX_DAYS", "7"))

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
PER_OFFICE_DELAY = float(os.environ.get("PER_OFFICE_DELAY", "3"))
RUN_ONCE = os.environ.get("RUN_ONCE", "") == "1"

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")  # only needed for self-hosted/auth ntfy

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))

# Daily "still alive" message hour (0-23, America/Denver). -1 disables.
HEARTBEAT_HOUR = int(os.environ.get("HEARTBEAT_HOUR", "9"))

# Send a warning notification after this many consecutive failed cycles.
ERROR_ALERT_AFTER = int(os.environ.get("ERROR_ALERT_AFTER", "12"))

DENVER = ZoneInfo("America/Denver")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

log = logging.getLogger("dmv-monitor")

# --------------------------------------------------------------------------
# HTML parsing helpers (the app is server-rendered ASP.NET MVC)
# --------------------------------------------------------------------------

INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')
OPTION_RE = re.compile(
    r'<div[^>]*class="[^"]*QflowObjectItem[^"]*"[^>]*data-id="(\d+)"[^>]*>\s*<p>\s*([^<]*?)\s*</p>',
    re.I,
)
FORM_ACTION_RE = re.compile(r'<form[^>]*action="([^"]*)"[^>]*method="post"', re.I)
DATES_RE = re.compile(r"var\s+Dates\s*=\s*\[(.*?)\]", re.S)
DATE_RE = re.compile(r'"(\d{4}-\d{2}-\d{2})"')
SLOT_RE = re.compile(r'data-datetime="([^"]+)"')


def parse_form(page):
    """Return (action, fields) for the wizard <form> on the page."""
    m = FORM_ACTION_RE.search(page)
    if not m:
        raise ValueError("no wizard form found on page")
    action = html.unescape(m.group(1))
    fields = {}
    for tag in INPUT_RE.findall(page):
        attrs = dict(ATTR_RE.findall(tag))
        if attrs.get("type", "").lower() != "hidden":
            continue
        name = attrs.get("name")
        if name:
            fields[name] = html.unescape(attrs.get("value", ""))
    if "formJourney" not in fields:
        raise ValueError("wizard form is missing formJourney state field")
    return action, fields


def parse_options(page):
    """Return {id: label} for QflowObjectItem selection buttons."""
    return {int(i): html.unescape(label) for i, label in OPTION_RE.findall(page)}


def parse_availability(page):
    """Return (sorted date list, time slots for the earliest date)."""
    m = DATES_RE.search(page)
    dates = sorted(set(DATE_RE.findall(m.group(1)))) if m else []
    slots = [s for s in SLOT_RE.findall(page) if s.strip()]
    return dates, sorted(set(slots))


# --------------------------------------------------------------------------
# DMV wizard client
# --------------------------------------------------------------------------


class WizardError(RuntimeError):
    pass


def new_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def post_step(session, action, fields, value):
    """Submit a wizard step with the chosen option and return the next page."""
    data = dict(fields)
    data["StepControls[0].Model.Value"] = str(value)
    resp = session.post(
        BASE_URL + action,
        data=data,
        headers={"Referer": BOOKING_URL, "Origin": BASE_URL},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def check_office(session, office_id):
    """Walk office -> service -> date step; return (dates, earliest_slots)."""
    resp = session.get(BOOKING_URL, timeout=30)
    resp.raise_for_status()
    action, fields = parse_form(resp.text)

    offices = parse_options(resp.text)
    if office_id not in offices:
        raise WizardError(f"office id {office_id} not offered on office-selection step")

    page = post_step(session, action, fields, office_id)
    action, fields = parse_form(page)

    services = parse_options(page)
    service_id = next(
        (i for i, label in services.items() if SERVICE_MATCH in label.lower()),
        SERVICE_ID_FALLBACK if SERVICE_ID_FALLBACK in services else None,
    )
    if service_id is None:
        raise WizardError(
            f"no service matching {SERVICE_MATCH!r} at office {office_id}; "
            f"offered: {services}"
        )

    page = post_step(session, action, fields, service_id)
    dates, slots = parse_availability(page)
    if not dates and "Dates" not in page:
        raise WizardError(f"date step for office {office_id} had no Dates array")
    return dates, slots


# --------------------------------------------------------------------------
# Notifications (ntfy)
# --------------------------------------------------------------------------


def notify(title, body, priority="high", tags="rotating_light,car", click=BOOKING_URL):
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Click": click,
    }
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    try:
        r = requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        log.info("notification sent: %s", title)
    except Exception:
        log.exception("failed to send ntfy notification")


# --------------------------------------------------------------------------
# State (which slots we already alerted on)
# --------------------------------------------------------------------------


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": {}, "last_heartbeat": ""}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


def office_name(office_id):
    return OFFICE_NAMES.get(office_id, f"Office {office_id}")


def fmt_slot_times(slots, max_n=6):
    times = []
    for s in slots:
        try:
            times.append(datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p").strftime("%-I:%M %p"))
        except ValueError:
            times.append(s)
    shown = ", ".join(times[:max_n])
    if len(times) > max_n:
        shown += f" (+{len(times) - max_n} more)"
    return shown


def run_cycle(state):
    """Check all offices once. Returns True if every office check succeeded."""
    today = datetime.now(DENVER).date()
    lo = today + timedelta(days=MIN_DAYS)
    hi = today + timedelta(days=MAX_DAYS)

    hits = []       # (office_id, [matching dates], earliest_slots)
    summary = []    # for logs/heartbeat
    all_ok = True

    for oid in OFFICE_IDS:
        try:
            dates, slots = check_office(new_session(), oid)
        except Exception as e:
            all_ok = False
            log.warning("check failed for %s: %s", office_name(oid), e)
            time.sleep(PER_OFFICE_DELAY)
            continue

        matching = [d for d in dates if lo <= datetime.strptime(d, "%Y-%m-%d").date() <= hi]
        earliest = dates[0] if dates else "none"
        summary.append(f"{office_name(oid)}: earliest {earliest}")
        log.info(
            "%s: %d dates, earliest %s, in-window %s",
            office_name(oid), len(dates), earliest, matching or "none",
        )

        notified = state["notified"].setdefault(str(oid), {})
        # Forget dates that are no longer available (or now in the past) so a
        # cancellation that reopens a date triggers a fresh alert.
        current = set(matching)
        for d in list(notified):
            if d not in current:
                del notified[d]

        new_dates = [d for d in matching if d not in notified]
        if new_dates:
            hits.append((oid, new_dates, slots if dates and dates[0] in new_dates else []))
            now_iso = datetime.now(DENVER).isoformat()
            for d in new_dates:
                notified[d] = now_iso

        time.sleep(PER_OFFICE_DELAY)

    if hits:
        lines = []
        for oid, new_dates, slots in hits:
            pretty = ", ".join(
                datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d") for d in new_dates
            )
            line = f"{office_name(oid)}: {pretty}"
            if slots:
                line += f" — times: {fmt_slot_times(slots)}"
            lines.append(line)
        n = sum(len(h[1]) for h in hits)
        notify(
            f"DMV: {n} early renewal slot date{'s' if n > 1 else ''} open!",
            "\n".join(lines) + "\n\nBook fast — tap to open the DMV scheduler.",
            priority="urgent",
        )

    # Daily heartbeat so you know the monitor is alive.
    if HEARTBEAT_HOUR >= 0:
        now = datetime.now(DENVER)
        today_s = now.date().isoformat()
        if now.hour >= HEARTBEAT_HOUR and state.get("last_heartbeat") != today_s:
            notify(
                "DMV monitor is running",
                "Watching for renewal slots %d-%d days out.\n%s"
                % (MIN_DAYS, MAX_DAYS, "\n".join(summary) or "No offices checked yet."),
                priority="min",
                tags="white_check_mark",
            )
            state["last_heartbeat"] = today_s

    save_state(state)
    return all_ok


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC is not set — alerts would have nowhere to go. "
                 "Set it in .env (or as a repo secret when using GitHub Actions).")
    log.info(
        "watching offices %s | window %d-%d days | every ~%ds | ntfy topic %r",
        [office_name(o) for o in OFFICE_IDS], MIN_DAYS, MAX_DAYS, POLL_SECONDS, NTFY_TOPIC,
    )

    stop = {"flag": False}

    def on_signal(signum, frame):
        stop["flag"] = True
        log.info("signal %s received, shutting down", signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    state = load_state()
    # Persisted so RUN_ONCE mode (e.g. GitHub Actions) can detect sustained
    # failure across separate process invocations.
    consecutive_errors = int(state.get("consecutive_errors", 0))

    while True:
        ok = run_cycle(state)
        if ok:
            consecutive_errors = 0
        else:
            consecutive_errors += 1
            if consecutive_errors == ERROR_ALERT_AFTER:
                notify(
                    "DMV monitor: checks are failing",
                    f"{consecutive_errors} polling cycles in a row had errors. "
                    "The DMV site may have changed or is blocking requests — "
                    "check the monitor logs.",
                    priority="default",
                    tags="warning",
                )
        state["consecutive_errors"] = consecutive_errors
        save_state(state)

        if RUN_ONCE or stop["flag"]:
            break
        # Jitter the interval so polling doesn't look mechanical.
        time.sleep(max(30, POLL_SECONDS + random.uniform(-0.2, 0.2) * POLL_SECONDS))
        if stop["flag"]:
            break


if __name__ == "__main__":
    main()
