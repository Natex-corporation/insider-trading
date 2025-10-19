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

## Continuous update watcher for long-running containers

For a lightweight alternative to the full LXC deployment script, you can run
`scripts/auto_update.py` alongside the bot. The watcher polls the remote
`main` branch, pulls new commits, and restarts `main.py` whenever updates are
available.

```bash
# Optional, required only for private repositories
export GITHUB_PAT="<your-token>"

python3 scripts/auto_update.py --interval 300
```

Use the `--interval` flag (seconds) to control how often the watcher checks for
updates, and `--command` if you need to launch a different entry point than the
default `python3 main.py`.

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
- Persist the deployment branch so the service tracks the same remote branch on every restart.
- Automatically fetch and hard reset to the tracked branch before launching the bot, keeping the deployment up to date.

The selected branch name is stored in `/etc/${SERVICE_NAME}.env` and exposed to the service as the `SERVICE_BRANCH` environment variable. You can rerun the setup script with a different `BRANCH_NAME` to switch tracks; the service will follow the new branch and pull updates automatically on subsequent restarts.

Afterwards, you can view live logs with:

```bash
journalctl -fu insider-trading.service
```

## Disclaimer
This repository is for educational purposes only. Trading in financial markets carries risk; use at your own discretion.
