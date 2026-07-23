# Colorado DMV Slot Monitor

Watches the Colorado DMV online appointment system for **driver-license
renewal** openings at your chosen offices, and pushes an alert to your phone
(via the free [ntfy](https://ntfy.sh) app) the moment a date **1–7 days from
today** opens up — usually a cancellation. The alert links straight to the
DMV scheduler so you can grab the slot.

It only reads availability (office → service → date list). It never books,
holds, or touches the customer-info step, and it polls politely (default:
one pass over all offices every ~5 minutes).

## 1. Phone setup (2 minutes, free)

1. Install **ntfy** — [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iPhone](https://apps.apple.com/us/app/ntfy/id1625396347).
2. In the app tap **+ / Subscribe to topic** and enter your private topic
   name — the same value you set as `NTFY_TOPIC`.
3. That's it.

The topic name acts as the password (anyone who knows it could see the
alerts), so use a random, hard-to-guess name and don't publish it. Alerts
contain no personal info — just office names and dates.

## 2. Run it

### Option A — GitHub Actions (free, no server, no card)

A scheduled workflow (`.github/workflows/check.yml`) runs the checker every
~10 minutes on GitHub's dime. Setup, from a fork/copy of this repo:

1. Keep the repo **public** (public repos get unlimited free Actions
   minutes; on a private repo this schedule would exhaust the free
   2,000 min/month quota partway through the month).
2. Add your topic as a secret:
   `gh secret set NTFY_TOPIC --body "<your-topic>"`
   (or repo **Settings → Secrets and variables → Actions → New secret**).
3. Enable workflows on the **Actions** tab, then trigger **DMV slot check →
   Run workflow** once to verify.

Dedup state is committed back to the repo as `state.json` after each run.
Note: GitHub's scheduler is best-effort — "every 10 minutes" lands every
10–20 minutes in practice, and GitHub pauses schedules in repos with no
activity for 60 days (the state commits normally keep it active; if alerts
ever stop, check the Actions tab for a "re-enable" banner).

### Option B — Docker (recommended on a VPS)

```bash
git clone <this folder> dmv-slot-monitor && cd dmv-slot-monitor   # or scp/rsync the folder
cp .env.example .env      # edit if you want different offices/window
docker compose up -d --build
docker compose logs -f    # watch it work
```

### Option C — plain Python + systemd (no Docker)

```bash
sudo cp -r dmv-slot-monitor /opt/dmv-slot-monitor
cd /opt/dmv-slot-monitor
cp .env.example .env
sudo apt install -y python3-requests    # or: pip3 install -r requirements.txt
sudo cp dmv-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dmv-monitor
journalctl -u dmv-monitor -f            # watch it work
```

### Quick local test (any machine with Python 3.9+)

```bash
RUN_ONCE=1 python3 monitor.py
```

## 3. Where to host

Any always-on Linux box works — it needs almost no CPU/RAM:

| Host | Cost | Notes |
|---|---|---|
| **Oracle Cloud "Always Free"** | $0 | Free forever ARM/AMD VM. Sign-up is the only hassle. |
| **DigitalOcean / Vultr / Hetzner** | ~$4–6/mo | 5-minute setup, pick the cheapest droplet, Ubuntu 24.04. |
| A spare PC / Raspberry Pi at home | $0 | Works fine; just has to stay on. |

On a fresh Ubuntu VPS the whole deploy is: install Docker
(`curl -fsSL https://get.docker.com | sh`), copy this folder up
(`scp -r dmv-slot-monitor user@server:`), then Option A above.

## 4. Configuration

Everything is set in `.env` (see `.env.example`). Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `OFFICE_IDS` | `44,12,10,14,29` | Westminster, Denver NE, Aurora, Centennial, Loveland |
| `MIN_DAYS` / `MAX_DAYS` | `1` / `7` | Alert window, in days from today (Denver time) |
| `POLL_SECONDS` | `300` | Delay between polling cycles (don't go below ~120 — be polite) |
| `NTFY_TOPIC` | *(required)* | Your private alert channel |
| `HEARTBEAT_HOUR` | `9` | Daily "still alive" push (set `-1` to turn off) |
| `SERVICE_MATCH` | `renew colorado driver license` | Watch a different service by changing this text |

Office ids you can add to `OFFICE_IDS`: 81 Adams (Westminster/Pecos),
91 Denver Regional Service Center, 85 Boulder, 92 Longmont, 13 Golden,
20 Parker, 24 Fort Collins, 27 Greeley.

## 5. What an alert looks like

> **DMV: 2 early renewal slot dates open!** 🚨
> Westminster: Fri Jul 24, Mon Jul 27 — times: 11:15 AM, 1:30 PM
> Centennial: Tue Jul 28
>
> Book fast — tap to open the DMV scheduler.

Tapping the notification opens the DMV booking page. You'll also get:

- one **low-priority heartbeat per day** (~9am) so you know it's alive, and
- a **warning** if checks fail for ~an hour straight (site changed/blocking).

Each open date is alerted **once**; if it disappears (someone took it) and
later reopens, you're alerted again.

## 6. How it works / if it breaks

The DMV scheduler (Q-Flow at `coloradoappt.cxmflow.com`) renders each wizard
step as HTML. The monitor walks office → service ("Renew Colorado Driver
License/ID/Permit") → date step and reads the embedded `var Dates = [...]`
availability array. No login or CAPTCHA is involved. If the DMV changes
platforms someday, the monitor will start logging errors and send you the
"checks are failing" warning rather than dying silently.
