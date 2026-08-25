from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config  # noqa: E402


def main() -> int:
    config = load_config()
    if config.monitoring_enabled:
        url = f"http://127.0.0.1:{config.monitoring_port}/healthz"
        try:
            with urlopen(url, timeout=5) as response:
                return 0 if response.status == 200 else 1
        except URLError:
            return 1

    if not config.heartbeat_file.exists():
        return 1

    heartbeat_age_seconds = __import__("time").time() - config.heartbeat_file.stat().st_mtime
    return 0 if heartbeat_age_seconds <= config.health_max_age_seconds else 1


if __name__ == "__main__":
    raise SystemExit(main())
