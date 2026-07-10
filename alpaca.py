import os
import sys

import requests


API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def _normalize_alpaca_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    return value[:-3] if value.endswith("/v2") else value


BASE_URL = _normalize_alpaca_base_url(os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))


def main() -> int:
    if not API_KEY or not SECRET_KEY:
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this helper.", file=sys.stderr)
        return 1

    # Connectivity checks must never create an order. This helper intentionally
    # calls the read-only account and market-clock endpoints only.
    account_url = f"{BASE_URL.rstrip('/')}/v2/account"
    clock_url = f"{BASE_URL.rstrip('/')}/v2/clock"
    headers = {
        "accept": "application/json",
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    account_response = requests.get(account_url, headers=headers, timeout=30)
    account_response.raise_for_status()
    clock_response = requests.get(clock_url, headers=headers, timeout=30)
    clock_response.raise_for_status()
    account = account_response.json()
    clock = clock_response.json()
    print(
        "Connection OK: "
        f"status={account.get('status', 'unknown')}, "
        f"trading_blocked={account.get('trading_blocked', 'unknown')}, "
        f"market_open={clock.get('is_open', 'unknown')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
