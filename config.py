from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    raise ValueError(f"Missing required environment variable: {name}")


def _normalize_alpaca_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    return value[:-3] if value.endswith("/v2") else value


def _resolve_path(raw: str, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


@dataclass(frozen=True)
class AppConfig:
    request_timeout: tuple[int, int]
    state_dir: Path
    log_dir: Path
    sqlite_db_path: Path
    heartbeat_file: Path
    app_log_file: Path
    trade_history_csv: Path
    seen_trades_log: Path
    pending_orders_json: Path
    trade_capital_czk: float
    take_profit_percent: float
    insider_scan_interval_minutes: int
    position_check_interval_minutes: int
    market_open_poll_seconds: int
    market_closed_poll_seconds: int
    api_key: str
    secret_key: str
    base_url: str
    monitoring_enabled: bool
    monitoring_host: str
    monitoring_port: int
    health_max_age_seconds: int


def load_config() -> AppConfig:
    request_timeout = (
        _env_int("REQUEST_TIMEOUT_CONNECT_SECONDS", 10),
        _env_int("REQUEST_TIMEOUT_READ_SECONDS", 25),
    )

    state_dir = _resolve_path(_env_str("STATE_DIR", "."), PROJECT_ROOT)
    log_dir = _resolve_path(_env_str("LOG_DIR", "."), state_dir)

    trade_capital_czk = _env_float("TRADE_CAPITAL_CZK", 250.0)
    insider_scan_interval_minutes = _env_int("INSIDER_SCAN_INTERVAL_MINUTES", 5)
    position_check_interval_minutes = _env_int("POSITION_CHECK_INTERVAL_MINUTES", 2)
    market_open_poll_seconds = _env_int("MARKET_OPEN_POLL_SECONDS", 30)
    market_closed_poll_seconds = _env_int(
        "MARKET_CLOSED_POLL_SECONDS",
        insider_scan_interval_minutes * 60,
    )

    default_health_max_age_seconds = max(
        market_open_poll_seconds,
        market_closed_poll_seconds,
    ) + request_timeout[1] + 300

    config = AppConfig(
        request_timeout=request_timeout,
        state_dir=state_dir,
        log_dir=log_dir,
        sqlite_db_path=_resolve_path(_env_str("SQLITE_DB_PATH", "insider_trading.sqlite3"), state_dir),
        heartbeat_file=_resolve_path(_env_str("HEARTBEAT_FILE", "heartbeat.txt"), state_dir),
        app_log_file=_resolve_path(_env_str("APP_LOG_FILE", "app.log"), log_dir),
        trade_history_csv=_resolve_path(_env_str("TRADE_HISTORY_CSV", "trade_history.csv"), state_dir),
        seen_trades_log=_resolve_path(_env_str("SEEN_TRADES_LOG", "seen_insider_trades.log"), state_dir),
        pending_orders_json=_resolve_path(_env_str("PENDING_ORDERS_JSON", "pending_orders.json"), state_dir),
        trade_capital_czk=trade_capital_czk,
        take_profit_percent=_env_float("TAKE_PROFIT_PERCENT", 10.0),
        insider_scan_interval_minutes=insider_scan_interval_minutes,
        position_check_interval_minutes=position_check_interval_minutes,
        market_open_poll_seconds=market_open_poll_seconds,
        market_closed_poll_seconds=market_closed_poll_seconds,
        api_key=_required_env("ALPACA_API_KEY"),
        secret_key=_required_env("ALPACA_SECRET_KEY"),
        base_url=_normalize_alpaca_base_url(
            _env_str("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        ),
        monitoring_enabled=_env_bool("MONITORING_ENABLED", True),
        monitoring_host=_env_str("MONITORING_HOST", "0.0.0.0"),
        monitoring_port=_env_int("MONITORING_PORT", 8080),
        health_max_age_seconds=_env_int(
            "HEALTH_MAX_AGE_SECONDS",
            default_health_max_age_seconds,
        ),
    )

    for directory in {
        config.state_dir,
        config.log_dir,
        config.sqlite_db_path.parent,
        config.heartbeat_file.parent,
        config.app_log_file.parent,
        config.trade_history_csv.parent,
        config.seen_trades_log.parent,
        config.pending_orders_json.parent,
    }:
        directory.mkdir(parents=True, exist_ok=True)

    return config
