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

RUN groupadd --gid 10001 insider \
    && useradd --uid 10001 --gid insider --create-home --shell /usr/sbin/nologin insider \
    && mkdir -p /data/logs \
    && chown -R insider:insider /data

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "main.py"]
