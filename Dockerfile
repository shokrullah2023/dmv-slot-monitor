FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .

ENV STATE_FILE=/data/state.json
VOLUME /data

CMD ["python", "-u", "monitor.py"]
