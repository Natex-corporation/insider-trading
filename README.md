# Insider Edge

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Testing: Pytest](https://img.shields.io/badge/tests-pytest-0A9EDC.svg?logo=pytest&logoColor=white)](tests/)

**Insider Edge** is an automated, safety-first Python algorithmic trading and telemetry service. It tracks high-conviction SEC Form 4 insider transactions, filters actionable market signals, enforces multi-layered portfolio risk guardrails, executes paper trades via the Alpaca API, and serves a live web control room with Prometheus telemetry and benchmark tracking.

Designed for robust self-hosting on Linux, Docker, and TrueNAS SCALE with zero data-loss SQLite persistence and strict non-root process hardening.

---

## 🏛️ System Architecture

`mermaid
flowchart TD
    subgraph Ingestion ["Data Ingestion & Signal Extraction"]
        A[Finviz Insider Feed] -->|Scrape Recent Form 4s| B[Signal Deduplication & Freshness Filter]
        B -->|Reject Stale & Non-Open Market| C[Signal Screener]
    end

    subgraph RiskEngine ["Risk & Policy Engine"]
        C --> D{Risk Guardrails}
        D -->|Check Daily Entry Cap| D1[Daily Limit Check]
        D -->|Check Open Position Limit| D2[Position Cap Check]
        D -->|Check Gross Exposure USD| D3[Exposure Check]
        D -->|Check Dry Run & Mode| D4[Mode Verification]
    end

    subgraph Execution ["Order & State Engine"]
        D4 -->|Qualified Signals| E[SQLite Transaction Queue]
        E -->|Deterministic Client Order ID| F[Alpaca Trading API]
        F -->|Fills & Updates| G[(SQLite ACID Store)]
        G -->|Active Positions| H[Exit Monitor: TP / SL / Max Hold]
        H -->|Exit Orders| F
    end

    subgraph Telemetry ["Operations & Web Control Room"]
        G --> I[Starlette Monitoring Server]
        F -->|Account Equity / Benchmark| I
        I --> J[Web Dashboard :8080]
        I --> K[JSON Status /status]
        I --> L[Prometheus Metrics /metrics]
        I --> M[Health Probes /healthz & /readyz]
    end
`

---

## ✨ Key Features

- **Automated SEC Form 4 Ingestion**: Continuously scrapes and parses real-time insider purchases and sales from Finviz with configurable page depth and lookback freshness thresholds.
- **Multi-Layered Risk Management**:
  - **Account & Position Caps**: Configurable maximum open positions (MAX_OPEN_POSITIONS) and daily entry throttle (MAX_NEW_ENTRIES_PER_DAY).
  - **Exposure Bounds**: Hard limits on gross dollar exposure (MAX_GROSS_EXPOSURE_USD) including queued orders.
  - **Dry-Run Default**: Safe-by-default execution (DRY_RUN=true) to evaluate signals and test pipelines without risking capital.
  - **Directional Safety**: Short-selling protection (ALLOW_SHORTING=false) ensuring sales are monitored or closed without unsolicited short exposure.
- **Deterministic Execution & Position Lifecycle**:
  - Automatically calculates position size based on target CZK/USD capital allocation.
  - Tracks fills with deterministic client order IDs to prevent duplicate executions.
  - Built-in position lifecycle manager handling take-profit, stop-loss, and maximum holding period exits.
- **ACID SQLite Persistence**:
  - Stores all signals, queued orders, executions, fills, exits, and historical performance metrics.
  - Automatic in-place database migrations and backward compatibility exports (	rade_history.csv, seen_insider_trades.log).
- **Live Web Control Room & Telemetry**:
  - **Control Dashboard (/)**: Real-time view of market status, active positions, 24-hour signal funnel, queue state, and historical win-rate leaderboard.
  - **Benchmark Tracking**: Live comparison of account equity return against buy-and-hold SPY (S&P 500).
  - **Prometheus Metrics (/metrics)**: Production-ready metrics for scraping health, order latency, queue depth, and position count.
  - **Health & Readiness Endpoints (/healthz, /readyz)**: Container liveness probes verifying broker connectivity and SQLite integrity.
- **Production Hardened**:
  - Dedicated non-root container execution (UID/GID 10001).
  - Read-only root filesystem support with explicit state volume isolation (/data).
  - TrueNAS SCALE deployment template and compose generation script.

---

## 🖥️ Web Control Room

The embedded web server runs on port 8080 (configurable via MONITORING_PORT):

| Endpoint | Auth Required | Description |
|---|:---:|---|
| / | Optional (MONITORING_TOKEN) | Responsive visual control dashboard with performance charts, active signals, and funnel analytics |
| /status | Optional (MONITORING_TOKEN) | Complete operational state snapshot in structured JSON |
| /metrics | Optional (MONITORING_TOKEN) | Prometheus text-format telemetry for Grafana / alert managers |
| /healthz | No | Process liveness check for Docker / Kubernetes probes |
| /readyz | No | Storage accessibility and broker clock readiness check |

---

## 🚀 Quickstart

### Prerequisites

- Python 3.11 or higher
- Alpaca Paper Trading API keys ([Sign up for Alpaca](https://alpaca.markets/))

### 1. Local Setup

`ash
# Clone repository
git clone https://github.com/Natex-corporation/insider-trading.git
cd insider-trading

# Create and activate virtual environment
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows (PowerShell):
# .venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
`

Edit .env and provide your Alpaca credentials:

`env
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DRY_RUN=true
`

Run the application:

`ash
# Verify broker connectivity (read-only test)
python alpaca.py

# Launch service
python main.py
`

Visit http://localhost:8080 to access the control dashboard.

---

### 2. Docker Setup

`ash
# Prepare data directory
mkdir -p data

# Start container with Docker Compose
docker compose up -d --build

# View real-time logs
docker compose logs -f
`

---

### 3. TrueNAS SCALE Deployment

Insider Edge includes dedicated automation for TrueNAS SCALE custom apps:

1. Generate the tailored TrueNAS compose file:
   `ash
   export ALPACA_API_KEY="your-paper-key"
   export ALPACA_SECRET_KEY="your-paper-secret"
   export TRUENAS_DATASET_PATH="/mnt/pool/apps/insider-trading"
   python scripts/render_truenas_compose.py
   `
2. Paste the generated 	ruenas-compose.generated.yaml into **TrueNAS > Apps > Install via YAML**.
3. See [TRUENAS_DEPLOY.md](TRUENAS_DEPLOY.md) for step-by-step permissions and storage configuration.

---

## ⚙️ Configuration Reference

All settings can be configured via environment variables or .env:

| Variable | Default | Description |
|---|:---:|---|
| ALPACA_API_KEY | *required* | Alpaca API Key ID |
| ALPACA_SECRET_KEY | *required* | Alpaca Secret Key |
| ALPACA_BASE_URL | https://paper-api.alpaca.markets | Alpaca REST API endpoint |
| DRY_RUN | 	rue | When true, evaluates signals without submitting broker orders |
| ALLOW_SHORTING | alse | Permit eligible insider sales to initiate short positions |
| ALLOW_LIVE_TRADING | alse | Explicit opt-in safety guard to allow live endpoints |
| TRADE_CAPITAL_CZK | 250 | Target capital allocation per entry (converted via live FX) |
| TAKE_PROFIT_PERCENT | 10 | Automatic take-profit target above entry price (%) |
| STOP_LOSS_PERCENT | 7 | Automatic stop-loss limit below entry price (%) |
| MAX_HOLD_DAYS | 30 | Maximum position holding duration (days) |
| SIGNAL_MAX_AGE_HOURS | 36 | Maximum age of Finviz insider filings accepted |
| FINVIZ_MAX_PAGES | 3 | Maximum pages scanned per scraping cycle |
| MAX_OPEN_POSITIONS | 10 | Maximum concurrent positions (  for unlimited) |
| MAX_NEW_ENTRIES_PER_DAY | 10 | Maximum new trade entries per New York market day |
| MAX_GROSS_EXPOSURE_USD | 2500 | Hard cap on total portfolio gross market value |
| BENCHMARK_SYMBOL | SPY | Benchmark symbol for equity alpha tracking |
| STATE_DIR | /data | Directory for persistent SQLite database and logs |
| MONITORING_PORT | 8080 | Port for the web control dashboard and metrics server |
| MONITORING_TOKEN | *empty* | Optional bearer token to secure dashboard & telemetry |

---

## 🧪 Testing & Quality Assurance

The codebase includes a comprehensive unit test suite covering configuration validation, signal screening, risk checks, queue retry backoffs, and SQLite persistence.

`ash
# Install development dependencies
pip install -r requirements-dev.txt

# Run static analysis and linting
ruff check .

# Execute test suite
pytest
`

---

## 📊 Portfolio & Engineering Highlights

- **Resilient Polling & Backoff**: Handles market closures, weekends, broker outages, and rate limits with dynamic loop intervals and exponential backoff retry queues.
- **Zero Drift State Machine**: SQLite ACID state tracks each order across pending_new -> filled -> open_position -> exit_triggered -> closed, ensuring consistent state even across container restarts.
- **Strict Least-Privilege Security**: Runs as an unprivileged user (insider, UID 10001) in a read-only container root environment with secrets isolated to environment variables.
- **Regulatory & Broker Compliance**: Complies with FINRA margin standards and modern Alpaca API specifications.

---

## ⚖️ Disclaimer

*This software is developed for educational, research, and paper-trading purposes. Algorithmic trading involves financial risk. Market data feeds and third-party web scrapers may experience interruptions or format changes. Always thoroughly test strategies in paper trading mode before considering live deployment.*

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
