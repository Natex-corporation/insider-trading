# Insider Trading Bot

This project demonstrates a simple trading bot that monitors recent insider trades from [Finviz](https://finviz.com/insidertrading.ashx) and executes corresponding trades using the Alpaca paper trading API.

## Features
- Scrapes insider trading activity from Finviz.
- Persists already-seen trades to avoid duplicate orders.
- Places market orders through Alpaca and logs each execution.
- Records trade history in CSV format for later analysis.

## Requirements
- Python 3.10+
- Packages: `pandas`, `requests`, `beautifulsoup4`, `alpaca-trade-api`
- An Alpaca paper trading account and API keys.

Install the Python dependencies with:

```bash
pip install pandas requests beautifulsoup4 alpaca-trade-api
```

## Usage
1. Configure your Alpaca API credentials in `main.py` or supply them via environment variables.
2. Run the bot:

```bash
python main.py
```

Trade activity is appended to `trade_history.csv` and seen trades are stored in `seen_insider_trades.log`.

## Disclaimer
This repository is for educational purposes only. Trading in financial markets carries risk; use at your own discretion.
