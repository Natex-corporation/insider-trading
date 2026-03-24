from __future__ import annotations

import argparse
import os
from pathlib import Path


DEFAULT_IMAGE = "ghcr.io/natex-corporation/insider-trading:latest"
DEFAULT_OUTPUT = "truenas-compose.generated.yaml"


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


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
    return f"""services:
  insider-trading:
    image: {image}
    container_name: insider-trading
    restart: unless-stopped
    environment:
      ALPACA_API_KEY: "{alpaca_api_key}"
      ALPACA_SECRET_KEY: "{alpaca_secret_key}"
      ALPACA_BASE_URL: "https://paper-api.alpaca.markets"
      TRADE_CAPITAL_CZK: "{trade_capital_czk}"
      TAKE_PROFIT_PERCENT: "{take_profit_percent}"
      INSIDER_SCAN_INTERVAL_MINUTES: "{insider_scan_interval_minutes}"
      POSITION_CHECK_INTERVAL_MINUTES: "{position_check_interval_minutes}"
      MARKET_OPEN_POLL_SECONDS: "{market_open_poll_seconds}"
      MARKET_CLOSED_POLL_SECONDS: "{market_closed_poll_seconds}"
      STATE_DIR: "/data"
      LOG_DIR: "/data/logs"
      SQLITE_DB_PATH: "/data/insider_trading.sqlite3"
      MONITORING_ENABLED: "true"
      MONITORING_HOST: "0.0.0.0"
      MONITORING_PORT: "8080"
    ports:
      - "{host_port}:8080"
    volumes:
      - {dataset_path}:/data
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a TrueNAS custom-app compose file for insider-trading."
    )
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
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
