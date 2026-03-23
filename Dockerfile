FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data \
    LOG_DIR=/data/logs \
    MONITORING_ENABLED=true \
    MONITORING_HOST=0.0.0.0 \
    MONITORING_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/logs

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "main.py"]
