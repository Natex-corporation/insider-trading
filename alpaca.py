import os
import sys

import requests


API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def _normalize_alpaca_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    return value[:-3] if value.endswith("/v2") else value


BASE_URL = _normalize_alpaca_base_url(
    os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
)


def main() -> int:
    if not API_KEY or not SECRET_KEY:
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this helper.", file=sys.stderr)
        return 1

    url = f"{BASE_URL.rstrip('/')}/v2/orders"
    payload = {
        "type": "market",
        "time_in_force": "day",
        "symbol": "AAPL",
        "qty": "1",
        "side": "buy",
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    print(response.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
