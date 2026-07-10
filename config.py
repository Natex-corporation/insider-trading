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
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


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


def _require_range(name: str, value: float, minimum: float, maximum: float) -> None:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}; got {value}")


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero; got {value}")


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
    stop_loss_percent: float
    max_hold_days: int
    insider_scan_interval_minutes: int
    position_check_interval_minutes: int
    market_open_poll_seconds: int
    market_closed_poll_seconds: int
    signal_max_age_hours: int
    finviz_max_pages: int
    dry_run: bool
    allow_shorting: bool
    allow_live_trading: bool
    max_open_positions: int
    max_new_entries_per_day: int
    max_gross_exposure_usd: float
    max_queue_attempts: int
    queue_retry_base_seconds: int
    queue_expiry_hours: int
    order_fill_timeout_seconds: int
    day_trade_warning_limit: int
    day_trade_window_business_days: int
    performance_refresh_minutes: int
    performance_lookback_days: int
    benchmark_symbol: str
    api_key: str
    secret_key: str
    base_url: str
    monitoring_enabled: bool
    monitoring_host: str
    monitoring_port: int
    monitoring_token: str
    health_max_age_seconds: int

    @property
    def is_paper_account(self) -> bool:
        return "paper-api.alpaca.markets" in self.base_url.lower()


def load_config() -> AppConfig:
    request_timeout = (
        _env_int("REQUEST_TIMEOUT_CONNECT_SECONDS", 10),
        _env_int("REQUEST_TIMEOUT_READ_SECONDS", 25),
    )
    state_dir = _resolve_path(_env_str("STATE_DIR", "."), PROJECT_ROOT)
    log_dir = _resolve_path(_env_str("LOG_DIR", "."), state_dir)

    values = {
        "trade_capital_czk": _env_float("TRADE_CAPITAL_CZK", 250.0),
        "take_profit_percent": _env_float("TAKE_PROFIT_PERCENT", 10.0),
        "stop_loss_percent": _env_float("STOP_LOSS_PERCENT", 7.0),
        "max_hold_days": _env_int("MAX_HOLD_DAYS", 30),
        "insider_scan_interval_minutes": _env_int("INSIDER_SCAN_INTERVAL_MINUTES", 5),
        "position_check_interval_minutes": _env_int("POSITION_CHECK_INTERVAL_MINUTES", 2),
        "market_open_poll_seconds": _env_int("MARKET_OPEN_POLL_SECONDS", 30),
        "market_closed_poll_seconds": _env_int("MARKET_CLOSED_POLL_SECONDS", 300),
        "signal_max_age_hours": _env_int("SIGNAL_MAX_AGE_HOURS", 36),
        "finviz_max_pages": _env_int("FINVIZ_MAX_PAGES", 3),
        "max_open_positions": _env_int("MAX_OPEN_POSITIONS", 10),
        "max_new_entries_per_day": _env_int("MAX_NEW_ENTRIES_PER_DAY", 10),
        "max_gross_exposure_usd": _env_float("MAX_GROSS_EXPOSURE_USD", 2500.0),
        "max_queue_attempts": _env_int("MAX_QUEUE_ATTEMPTS", 5),
        "queue_retry_base_seconds": _env_int("QUEUE_RETRY_BASE_SECONDS", 60),
        "queue_expiry_hours": _env_int("QUEUE_EXPIRY_HOURS", 24),
        "order_fill_timeout_seconds": _env_int("ORDER_FILL_TIMEOUT_SECONDS", 15),
        "day_trade_warning_limit": _env_int("DAY_TRADE_WARNING_LIMIT", 3),
        "day_trade_window_business_days": _env_int("DAY_TRADE_WINDOW_BUSINESS_DAYS", 5),
        "performance_refresh_minutes": _env_int("PERFORMANCE_REFRESH_MINUTES", 15),
        "performance_lookback_days": _env_int("PERFORMANCE_LOOKBACK_DAYS", 365),
        "monitoring_port": _env_int("MONITORING_PORT", 8080),
    }

    _require_positive("TRADE_CAPITAL_CZK", values["trade_capital_czk"])
    _require_range("TAKE_PROFIT_PERCENT", values["take_profit_percent"], 0.1, 500.0)
    _require_range("STOP_LOSS_PERCENT", values["stop_loss_percent"], 0.1, 99.0)
    for name in (
        "max_hold_days",
        "insider_scan_interval_minutes",
        "position_check_interval_minutes",
        "market_open_poll_seconds",
        "market_closed_poll_seconds",
        "signal_max_age_hours",
        "finviz_max_pages",
        "max_open_positions",
        "max_new_entries_per_day",
        "max_queue_attempts",
        "queue_retry_base_seconds",
        "queue_expiry_hours",
        "order_fill_timeout_seconds",
        "day_trade_window_business_days",
        "performance_refresh_minutes",
        "performance_lookback_days",
    ):
        _require_positive(name.upper(), values[name])
    _require_positive("MAX_GROSS_EXPOSURE_USD", values["max_gross_exposure_usd"])
    _require_range("DAY_TRADE_WARNING_LIMIT", values["day_trade_warning_limit"], 0, 1000)
    _require_range("MONITORING_PORT", values["monitoring_port"], 1, 65535)
    _require_positive("REQUEST_TIMEOUT_CONNECT_SECONDS", request_timeout[0])
    _require_positive("REQUEST_TIMEOUT_READ_SECONDS", request_timeout[1])

    default_health_max_age_seconds = (
        max(values["market_open_poll_seconds"], values["market_closed_poll_seconds"]) + request_timeout[1] + 120
    )

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
        dry_run=_env_bool("DRY_RUN", True),
        allow_shorting=_env_bool("ALLOW_SHORTING", False),
        allow_live_trading=_env_bool("ALLOW_LIVE_TRADING", False),
        benchmark_symbol=_env_str("BENCHMARK_SYMBOL", "SPY").upper(),
        api_key=_required_env("ALPACA_API_KEY"),
        secret_key=_required_env("ALPACA_SECRET_KEY"),
        base_url=_normalize_alpaca_base_url(_env_str("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")),
        monitoring_enabled=_env_bool("MONITORING_ENABLED", True),
        monitoring_host=_env_str("MONITORING_HOST", "0.0.0.0"),
        monitoring_token=_env_str("MONITORING_TOKEN", ""),
        health_max_age_seconds=_env_int("HEALTH_MAX_AGE_SECONDS", default_health_max_age_seconds),
        **values,
    )

    if not config.is_paper_account and not config.allow_live_trading:
        raise ValueError(
            "ALPACA_BASE_URL is not the paper endpoint. Set ALLOW_LIVE_TRADING=true only after an explicit live-trading review."
        )
    if not config.benchmark_symbol.replace("-", "").isalnum():
        raise ValueError("BENCHMARK_SYMBOL contains unsupported characters")

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
