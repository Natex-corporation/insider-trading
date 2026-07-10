# Insider Edge

Insider Edge is a safety-first Python service that watches recent insider transactions on
[Finviz](https://finviz.com/insidertrading.ashx), converts eligible activity into trade signals, and can mirror
those signals into an Alpaca paper-trading account.

It is an experimental strategy and operations dashboard, not evidence of a profitable system. Keep it in dry-run
or paper mode until its behavior and results are satisfactory.

## What it does

1. Reads the Alpaca market clock, account restrictions, buying power, assets, orders, and positions.
2. Scrapes only a bounded number of recent Finviz pages and rejects stale signals.
3. Filters option-related activity and optionally checks recent company-level insider direction.
4. Applies portfolio, daily-entry, shortability, signal-age, and exposure checks.
5. Submits deterministic client order IDs and reconciles orders to actual fills.
6. Manages only the quantity opened by this bot using take-profit, stop-loss, or maximum-hold exits.
7. Stores signals, queues, orders, fills, exits, and performance in SQLite.
8. Shows account performance against buy-and-hold SPY, risk usage, swing/day activity, and system health.

## Safety defaults

- `DRY_RUN=true`: signals are evaluated and reported, but no orders are submitted.
- Paper endpoint required unless `ALLOW_LIVE_TRADING=true` is explicitly set.
- `ALLOW_SHORTING=false`: an insider sale does not automatically create a short position.
- A fresh state directory records the first scrape as a baseline without trading it.
- Signals older than `SIGNAL_MAX_AGE_HOURS` are not considered.
- New entries stop when position, daily-entry, exposure, buying-power, or broker restrictions are reached.
- Failed queue entries use exponential backoff and become terminal after an attempt or age limit.
- If Alpaca account or position state cannot be verified, new entries and managed exits pause.

To enable paper orders after reviewing the dashboard:

```env
DRY_RUN=false
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Live trading requires a second, separate opt-in. Do not point this project at the live endpoint casually.

## Dashboard

The monitoring server listens on port `8080` by default.

- `/`: responsive control-room dashboard
- `/status`: complete JSON snapshot
- `/metrics`: Prometheus text metrics
- `/healthz`: process liveness
- `/readyz`: storage and broker-clock readiness

The dashboard includes:

- Alpaca account equity return versus buy-and-hold `SPY` over the same dates
- relative performance (account return minus benchmark return)
- equity, buying power, gross exposure, positions, and entry limits
- same-day round trips and closed/open swing-trade counts
- bounded pending-order queue and retry details
- insider outcome leaderboard based on closed trades
- per-stage operational health

The benchmark is intentionally an account-level comparison. If the Alpaca account contains manual or unrelated
trades, those affect the account line. Bot-only realized P/L is also retained in the SQLite performance summary.

Set `MONITORING_TOKEN` to protect the detailed dashboard, JSON, and metrics endpoints. Health endpoints remain
unauthenticated for container probes. With a token, open `http://host:8080/?token=...` or send an
`Authorization: Bearer ...` header.

## Day and swing activity

The service records whether a closed trade was opened and closed on the same New York market date or held
overnight. `DAY_TRADE_WARNING_LIMIT` is an informational threshold displayed in the dashboard; broker controls
remain authoritative and safety exits are never intentionally trapped merely to conserve a counter.

Alpaca removed its old PDT/day-trading fields on July 6, 2026 after the move to FINRA's intraday-margin standards.
Do not treat the historical three-in-five heuristic as a universal current broker rule. See
[Alpaca's migration notice](https://docs.alpaca.markets/us/changelog/2026-07-06-pdt-db49dba) and confirm the rules
that apply to the account and broker.

## Configuration

Copy `.env.example` to `.env` and set fresh paper credentials. Important settings:

| Setting | Default | Meaning |
|---|---:|---|
| `DRY_RUN` | `true` | Evaluate without submitting orders |
| `ALLOW_SHORTING` | `false` | Permit eligible insider-sale signals to open shorts |
| `ALLOW_LIVE_TRADING` | `false` | Permit a non-paper Alpaca endpoint |
| `TRADE_CAPITAL_CZK` | `250` | Target notional per entry |
| `TAKE_PROFIT_PERCENT` | `10` | Fill-based take-profit distance |
| `STOP_LOSS_PERCENT` | `7` | Fill-based stop-loss distance |
| `MAX_HOLD_DAYS` | `30` | Maximum position age |
| `SIGNAL_MAX_AGE_HOURS` | `36` | Oldest accepted Finviz signal |
| `FINVIZ_MAX_PAGES` | `3` | Maximum pages per scrape |
| `MAX_OPEN_POSITIONS` | `10` | Account position cap |
| `MAX_NEW_ENTRIES_PER_DAY` | `10` | New-entry cap per New York market date |
| `MAX_GROSS_EXPOSURE_USD` | `2500` | Maximum absolute account market value plus reserved entries |
| `MAX_QUEUE_ATTEMPTS` | `5` | Terminal retry threshold |
| `QUEUE_EXPIRY_HOURS` | `24` | Maximum queued-signal age |
| `BENCHMARK_SYMBOL` | `SPY` | Buy-and-hold comparison symbol |
| `MONITORING_TOKEN` | empty | Optional dashboard/API bearer token |

All numeric settings are validated at startup. See `.env.example` for the full list.

## Local run

Python 3.11 is the supported runtime.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
export DRY_RUN="true"
python main.py
```

On PowerShell, activate with `.venv\Scripts\Activate.ps1` and set variables with `$env:NAME="value"`.

`python alpaca.py` is a read-only connectivity check. It no longer submits a test order.

## Docker

```bash
mkdir -p data
sudo chown -R 10001:10001 data
docker compose up -d --build
```

The image runs as UID/GID `10001`, uses a read-only root filesystem, and writes state only under `/data`. Ensure the
host `./data` directory is writable by that identity before starting the container. The local `.env` and generated
TrueNAS YAML are excluded from the Docker build context.

## Persistent state

SQLite is the source of truth:

- `insider_trading.sqlite3`: signals, bounded queue, order/fill lifecycle, exits, metadata
- `trade_history.csv`: compatibility export
- `seen_insider_trades.log`: compatibility export
- `pending_orders.json`: compatibility queue view
- `heartbeat.txt`: latest liveness record only
- `logs/app.log`: rotating detailed logs

Existing databases are migrated in place. Keep backups of `/data` before deploying a new image.

## Tests and CI

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

Container publishing now depends on lint and tests passing. Runtime dependencies are pinned for reproducible builds.

## TrueNAS

See [TRUENAS_DEPLOY.md](TRUENAS_DEPLOY.md). Generated compose files contain credentials, are written with restrictive
permissions where supported, are Git/Docker ignored, and should be deleted after use when practical.

## Disclaimer

This project is for educational paper trading. Insider transactions are not inherently predictive, third-party HTML
and APIs can change, and automated trading can lose money. Broker restrictions and applicable law take precedence
over local counters or settings.
