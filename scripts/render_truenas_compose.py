from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


DEFAULT_IMAGE = "ghcr.io/natex-corporation/insider-trading:latest"
DEFAULT_OUTPUT = "truenas-compose.generated.yaml"


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _yaml_string(value: str) -> str:
    """JSON strings are valid YAML scalars and safely escape secrets and paths."""
    return json.dumps(str(value))


def build_yaml(
    *,
    image: str,
    dataset_path: str,
    alpaca_api_key: str,
    alpaca_secret_key: str,
    host_port: int,
    trade_capital_czk: str,
    take_profit_percent: str,
    insider_scan_interval_minutes: str,
    position_check_interval_minutes: str,
    market_open_poll_seconds: str,
    market_closed_poll_seconds: str,
) -> str:
    q = _yaml_string
    return f"""services:
  insider-trading:
    image: {q(image)}
    container_name: insider-trading
    restart: unless-stopped
    read_only: true
    environment:
      ALPACA_API_KEY: {q(alpaca_api_key)}
      ALPACA_SECRET_KEY: {q(alpaca_secret_key)}
      ALPACA_BASE_URL: "https://paper-api.alpaca.markets"
      DRY_RUN: {q(_env_or_default("DRY_RUN", "true"))}
      ALLOW_SHORTING: {q(_env_or_default("ALLOW_SHORTING", "false"))}
      ALLOW_LIVE_TRADING: "false"
      TRADE_CAPITAL_CZK: {q(trade_capital_czk)}
      TAKE_PROFIT_PERCENT: {q(take_profit_percent)}
      STOP_LOSS_PERCENT: {q(_env_or_default("STOP_LOSS_PERCENT", "7"))}
      MAX_HOLD_DAYS: {q(_env_or_default("MAX_HOLD_DAYS", "30"))}
      SIGNAL_MAX_AGE_HOURS: {q(_env_or_default("SIGNAL_MAX_AGE_HOURS", "36"))}
      FINVIZ_MAX_PAGES: {q(_env_or_default("FINVIZ_MAX_PAGES", "3"))}
      INSIDER_SCAN_INTERVAL_MINUTES: {q(insider_scan_interval_minutes)}
      POSITION_CHECK_INTERVAL_MINUTES: {q(position_check_interval_minutes)}
      MARKET_OPEN_POLL_SECONDS: {q(market_open_poll_seconds)}
      MARKET_CLOSED_POLL_SECONDS: {q(market_closed_poll_seconds)}
      MAX_OPEN_POSITIONS: {q(_env_or_default("MAX_OPEN_POSITIONS", "10"))}
      MAX_NEW_ENTRIES_PER_DAY: {q(_env_or_default("MAX_NEW_ENTRIES_PER_DAY", "10"))}
      MAX_GROSS_EXPOSURE_USD: {q(_env_or_default("MAX_GROSS_EXPOSURE_USD", "2500"))}
      BENCHMARK_SYMBOL: {q(_env_or_default("BENCHMARK_SYMBOL", "SPY"))}
      STATE_DIR: "/data"
      LOG_DIR: "/data/logs"
      SQLITE_DB_PATH: "/data/insider_trading.sqlite3"
      MONITORING_ENABLED: "true"
      MONITORING_HOST: "0.0.0.0"
      MONITORING_PORT: "8080"
      MONITORING_TOKEN: {q(_env_or_default("MONITORING_TOKEN", ""))}
    ports:
      - "{host_port}:8080"
    volumes:
      - {q(f"{dataset_path}:/data")}
    tmpfs:
      - /tmp
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a TrueNAS custom-app compose file for insider-trading.")
    parser.add_argument(
        "--dataset-path",
        default=os.getenv("TRUENAS_DATASET_PATH"),
        help="TrueNAS dataset mount path, for example /mnt/tank/apps/insider-trading",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output file path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"Container image to deploy. Default: {DEFAULT_IMAGE}",
    )
    parser.add_argument(
        "--alpaca-api-key",
        default=_env_or_default("ALPACA_API_KEY", "replace-me"),
        help="Alpaca API key. Defaults to ALPACA_API_KEY env var or replace-me.",
    )
    parser.add_argument(
        "--alpaca-secret-key",
        default=_env_or_default("ALPACA_SECRET_KEY", "replace-me"),
        help="Alpaca secret key. Defaults to ALPACA_SECRET_KEY env var or replace-me.",
    )
    parser.add_argument(
        "--host-port",
        type=int,
        default=int(_env_or_default("TRUENAS_HOST_PORT", "8080")),
        help="Host port for the monitoring UI. Default: 8080",
    )
    parser.add_argument(
        "--trade-capital-czk",
        default=_env_or_default("TRADE_CAPITAL_CZK", "250"),
        help="Per-trade budget in CZK. Default: 250",
    )
    parser.add_argument(
        "--take-profit-percent",
        default=_env_or_default("TAKE_PROFIT_PERCENT", "10"),
        help="Take profit percentage. Default: 10",
    )
    parser.add_argument(
        "--insider-scan-interval-minutes",
        default=_env_or_default("INSIDER_SCAN_INTERVAL_MINUTES", "5"),
        help="How often to scrape Finviz. Default: 5",
    )
    parser.add_argument(
        "--position-check-interval-minutes",
        default=_env_or_default("POSITION_CHECK_INTERVAL_MINUTES", "2"),
        help="How often to evaluate exits during market hours. Default: 2",
    )
    parser.add_argument(
        "--market-open-poll-seconds",
        default=_env_or_default("MARKET_OPEN_POLL_SECONDS", "30"),
        help="Loop sleep while the market is open. Default: 30",
    )
    parser.add_argument(
        "--market-closed-poll-seconds",
        default=_env_or_default("MARKET_CLOSED_POLL_SECONDS", "300"),
        help="Loop sleep while the market is closed. Default: 300",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dataset_path:
        raise SystemExit(
            "Provide --dataset-path or set TRUENAS_DATASET_PATH, for example /mnt/tank/apps/insider-trading"
        )

    output_path = Path(args.output)
    yaml_text = build_yaml(
        image=args.image,
        dataset_path=args.dataset_path,
        alpaca_api_key=args.alpaca_api_key,
        alpaca_secret_key=args.alpaca_secret_key,
        host_port=args.host_port,
        trade_capital_czk=args.trade_capital_czk,
        take_profit_percent=args.take_profit_percent,
        insider_scan_interval_minutes=args.insider_scan_interval_minutes,
        position_check_interval_minutes=args.position_check_interval_minutes,
        market_open_poll_seconds=args.market_open_poll_seconds,
        market_closed_poll_seconds=args.market_closed_poll_seconds,
    )
    output_path.write_text(yaml_text, encoding="utf-8")
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
    print(f"Wrote {output_path}")
    print("This file contains credentials. Keep it private and delete it after deployment if practical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
