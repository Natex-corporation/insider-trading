# Insider Trading Bot

This project demonstrates a simple trading bot that monitors recent insider trades from [Finviz](https://finviz.com/insidertrading.ashx) and executes corresponding trades using the Alpaca paper trading API.

## Features
- Scrapes insider trading activity from Finviz.
- Persists already-seen trades to avoid duplicate orders.
- Places market orders through Alpaca and logs each execution.
- Records trade history in CSV format for later analysis.

## Requirements
- Python 3.10+
- Dependencies listed in [`requirements.txt`](requirements.txt)
- An Alpaca paper trading account and API keys (currently hard-coded in `main.py`).

Install the Python dependencies with:

```bash
pip install -r requirements.txt
```

## Usage
1. Configure your Alpaca API credentials in `main.py` or supply them via environment variables.
2. Run the bot:

```bash
python main.py
```

Trade activity is appended to `trade_history.csv` and seen trades are stored in `seen_insider_trades.log`.

## Automated deployment to an LXC container
If you want the bot to auto-update and run inside an LXC container after every push to `main`, this repository includes a helper script. Run the following as `root` inside the container:

```bash
export REPO_URL="https://github.com/<your-org>/insider-trading.git"
# Optional overrides:
# export APP_DIR="/srv/insider-trading"
# export BRANCH_NAME="main"
# export SERVICE_NAME="insider-trading"

bash scripts/setup_lxc.sh
```

The script will:
- Install Git and Python tooling.
- Clone the repository into `APP_DIR` (defaults to `/srv/insider-trading`).
- Create a virtual environment and install dependencies from `requirements.txt`.
- Install a `systemd` service that runs the bot with automatic restarts.

Afterwards, you can view live logs with:

```bash
journalctl -fu insider-trading.service
```

## Disclaimer
This repository is for educational purposes only. Trading in financial markets carries risk; use at your own discretion.
