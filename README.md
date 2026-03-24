# Insider Trading Bot

This repository contains a long-running Python bot that watches recent insider trades on [Finviz](https://finviz.com/insidertrading.ashx) and mirrors them into an Alpaca paper-trading account.

## What This Project Is

At a high level, the bot does this in a loop:

1. Checks whether the US market is open through Alpaca.
2. Fetches the latest insider-trading table from Finviz.
3. Converts each row into a simple trade signal (`buy` or `sell`).
4. Ignores signals it has already processed.
5. Places a market order in Alpaca when the market is open, or queues it for later.
6. Tracks open positions and closes them when the configured take-profit target is reached.
7. Writes state into SQLite and exports compatibility files for easy inspection.

## What It Actually Trades

The strategy is intentionally simple:

- It uses a fixed per-trade budget in CZK (`TRADE_CAPITAL_CZK`, default `250`).
- It converts that budget to USD each cycle.
- It buys or sells based only on the Finviz transaction label.
- It uses a take-profit target (`TAKE_PROFIT_PERCENT`, default `10`).
- It does not use a stop loss.
- It does not score insider quality, company quality, liquidity, or risk.
- It avoids duplicate orders by recording processed insider-trade IDs.

This means it is closer to an automation prototype than a production trading system.

## Runtime Files

By default, the bot writes state into the repository directory. In the container setup, it writes to `/data`.

- `insider_trading.sqlite3`: primary application database.
- `trade_history.csv`: exported trade history snapshot.
- `seen_insider_trades.log`: exported seen-signal snapshot.
- `pending_orders.json`: exported queue snapshot, now including `entries` and `exits`.
- `heartbeat.txt`: loop and stage heartbeat log.
- `app.log`: rotating application log file.

## Monitoring

The bot now exposes a lightweight monitoring server. By default it listens on port `8080`.

- `/`: small HTML status page.
- `/healthz`: liveness endpoint for Docker health checks.
- `/readyz`: same heartbeat-based readiness signal.
- `/status`: JSON snapshot of the bot state.
- `/metrics`: Prometheus-style text metrics.

The status page now also shows queued orders and a simple insider-performance leaderboard based on recorded trade outcomes.

## Configuration

The bot now reads credentials and runtime settings from environment variables.

Required:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

Common optional settings:

- `ALPACA_BASE_URL` default: `https://paper-api.alpaca.markets`
- `TRADE_CAPITAL_CZK` default: `250`
- `TAKE_PROFIT_PERCENT` default: `10`
- `INSIDER_SCAN_INTERVAL_MINUTES` default: `5`
- `POSITION_CHECK_INTERVAL_MINUTES` default: `2`
- `MARKET_OPEN_POLL_SECONDS` default: `30`
- `MARKET_CLOSED_POLL_SECONDS` default: `300`
- `STATE_DIR` default: repository directory
- `LOG_DIR` default: repository directory
- `SQLITE_DB_PATH` default: `insider_trading.sqlite3` inside the state directory
- `MONITORING_ENABLED` default: `true`
- `MONITORING_PORT` default: `8080`

See [.env.example](.env.example) for a working container example.

Important: older revisions of this repository contained hard-coded Alpaca paper keys. If those keys were ever valid, revoke them.

## Local Python Run

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the bot:

```bash
ALPACA_API_KEY=... ALPACA_SECRET_KEY=... python main.py
```

## Docker Run

Build and start:

```bash
docker compose up -d --build
```

The included `docker-compose.yml`:

- builds the image from the local `Dockerfile`
- loads variables from `.env`
- persists runtime state in `./data`
- exposes the monitoring UI on port `8080`

## TrueNAS Deployment

This repository now includes two paths that fit a TrueNAS-style deployment:

### Option 1: Publish an image and deploy it as a custom app

Use the included GitHub Actions workflow at [.github/workflows/publish-image.yml](.github/workflows/publish-image.yml) to publish the image to GHCR on pushes to `main`.

Then use [truenas-compose.yaml](truenas-compose.yaml) as the template for your TrueNAS custom app:

- the default image is already set to `ghcr.io/natex-corporation/insider-trading:latest`
- replace `/mnt/POOLNAME/apps/insider-trading:/data` with a real dataset path on your NAS
- set your Alpaca credentials in the environment section

This is the cleanest approach for TrueNAS because the server pulls a ready-made image and keeps bot state in a mounted dataset.

If you want the YAML generated for you, use [scripts/render_truenas_compose.py](scripts/render_truenas_compose.py) and see [TRUENAS_DEPLOY.md](TRUENAS_DEPLOY.md).

### Option 2: Run it on a normal Docker host first

If you want to validate behavior before moving it to TrueNAS, use the local `docker-compose.yml`, then deploy the same image to TrueNAS later.

## Legacy LXC Deployment

The old LXC/systemd helper is still present in [scripts/setup_lxc.sh](scripts/setup_lxc.sh), but it now requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to be set before running the installer.

## Files Added For Container Migration

- [Dockerfile](Dockerfile): container image definition
- [docker-compose.yml](docker-compose.yml): local Docker deployment
- [truenas-compose.yaml](truenas-compose.yaml): TrueNAS custom-app template
- [scripts/healthcheck.py](scripts/healthcheck.py): health probe used by Docker
- [scripts/render_truenas_compose.py](scripts/render_truenas_compose.py): generates a TrueNAS-ready custom-app YAML
- [config.py](config.py): environment-driven config loader
- [monitoring.py](monitoring.py): health, status, and metrics server
- [storage.py](storage.py): SQLite-backed state, queue, and insider analytics

## Disclaimer

This repository is for educational use. It submits automated paper trades and depends on third-party websites and APIs that can change without notice. Use it only after reviewing the code and understanding the risks.
