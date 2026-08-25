from __future__ import annotations

import datetime as dt
import functools
import hashlib
import logging
import math
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from alpaca_trade_api.rest import APIError, REST
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import load_config
from monitoring import RuntimeState, start_monitoring_server
from storage import Storage


try:
    CONFIG = load_config()
except ValueError as exc:
    raise SystemExit(str(exc)) from exc

REQUEST_TIMEOUT = CONFIG.request_timeout
HEARTBEAT_FILE = CONFIG.heartbeat_file
APP_LOG_FILE = CONFIG.app_log_file

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
    file_handler = RotatingFileHandler(APP_LOG_FILE, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(file_handler)

log = logging.getLogger(__name__)
RUNTIME_STATE = RuntimeState(CONFIG.health_max_age_seconds)


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; InsiderEdge/2.0)"})
    return session


SESSION = make_session()
STORAGE = Storage(
    db_path=CONFIG.sqlite_db_path,
    trade_history_csv=CONFIG.trade_history_csv,
    seen_trades_log=CONFIG.seen_trades_log,
    pending_orders_json=CONFIG.pending_orders_json,
    log=log,
)

OPTIONS_NOISE_PATTERN = re.compile(r"(option|exercise|derivative|convert|conversion|grant|award)", re.IGNORECASE)
PROPOSED_TRANSACTION_PATTERN = re.compile(r"proposed", re.IGNORECASE)
COMPANY_HISTORY_ROW_LIMIT = 12
COMPANY_TREND_DOMINANCE_RATIO = 1.5
PROPOSED_TRANSACTION_WEIGHT = 0.25
ORDER_TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}


def heartbeat(stage: str, ok: bool = True, note: str = "") -> None:
    """Persist only the latest heartbeat; detailed history belongs in the rotating app log."""
    try:
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        line = f"{timestamp}\t{stage}\t{'OK' if ok else 'ERR'}\t{note}\n"
        temporary = Path(f"{HEARTBEAT_FILE}.tmp")
        temporary.write_text(line, encoding="utf-8")
        os.replace(temporary, HEARTBEAT_FILE)
    except Exception as exc:
        log.error("Failed to write heartbeat: %s", exc)
    finally:
        RUNTIME_STATE.record_heartbeat(stage, ok=ok, note=note)


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def object_value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def refresh_runtime_views(context: dict[str, Any] | None = None) -> None:
    RUNTIME_STATE.update_pending_orders(STORAGE.get_pending_summary())
    RUNTIME_STATE.set_trade_history_rows(STORAGE.count_trade_history())
    RUNTIME_STATE.set_queue_preview(STORAGE.get_queue_preview())
    RUNTIME_STATE.set_insider_leaderboard(STORAGE.get_insider_leaderboard())
    RUNTIME_STATE.set_signal_activity(STORAGE.get_signal_activity())
    RUNTIME_STATE.set_activity(
        STORAGE.get_trading_activity(
            business_days=CONFIG.day_trade_window_business_days,
            warning_limit=CONFIG.day_trade_warning_limit,
        )
    )
    if context is not None:
        RUNTIME_STATE.set_account(context["account_snapshot"])
        RUNTIME_STATE.set_risk(risk_snapshot(context))


def get_usd_per_czk() -> float | None:
    try:
        response = SESSION.get("https://api.exchangerate-api.com/v4/latest/CZK", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        rate = safe_float(response.json().get("rates", {}).get("USD"))
        if rate is None or rate <= 0:
            raise ValueError("response did not contain a positive USD rate")
        return rate
    except Exception as exc:
        log.error("Could not fetch CZK to USD exchange rate: %s", exc)
        return None


def normalize_utc_datetime(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def compute_sleep_seconds(
    *, is_market_open: bool, pending_orders: dict[str, Any], next_open_at: dt.datetime | None
) -> int:
    if is_market_open:
        return max(5, CONFIG.market_open_poll_seconds)
    configured = max(15, CONFIG.market_closed_poll_seconds)
    has_pending = bool(pending_orders.get("buy") or pending_orders.get("sell"))
    normalized = normalize_utc_datetime(next_open_at)
    if has_pending and normalized is not None:
        until_open = max(15, int((normalized - dt.datetime.now(dt.timezone.utc)).total_seconds()))
        return min(configured, until_open)
    return configured


def parse_finviz_date(raw: str) -> dt.date | None:
    text = raw.strip()
    try:
        return dt.datetime.strptime(text, "%b %d '%y").date()
    except ValueError:
        pass
    try:
        partial = dt.datetime.strptime(f"{text} 2000", "%b %d %Y").date()
    except ValueError:
        return None
    today = dt.date.today()
    candidate = partial.replace(year=today.year)
    if candidate > today + dt.timedelta(days=7):
        candidate = candidate.replace(year=today.year - 1)
    return candidate


def infer_direction(transaction: str) -> str | None:
    lowered = transaction.lower()
    if "buy" in lowered or "purchase" in lowered:
        return "buy"
    if "sell" in lowered or "sale" in lowered:
        return "sell"
    return None


def parse_numeric_text(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = str(raw).replace(",", "").replace("$", "").strip()
    if cleaned in {"", "-", "None", "nan"}:
        return None
    return safe_float(cleaned)


def signal_weight_for_transaction(transaction: str) -> float:
    return PROPOSED_TRANSACTION_WEIGHT if PROPOSED_TRANSACTION_PATTERN.search(transaction) else 1.0


def make_trade_id(parts: list[str]) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{parts[0]}-{parts[1]}-{digest}"


def fetch_company_insider_activity(ticker: str) -> list[dict[str, Any]]:
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not fetch company insider activity for %s: %s", ticker, exc)
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    records: list[dict[str, Any]] = []
    for row in soup.select("tr.fv-insider-row"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        owner = cells[0].get_text(" ", strip=True)
        relationship = cells[1].get_text(" ", strip=True) or None
        date = parse_finviz_date(cells[2].get_text(" ", strip=True))
        transaction = cells[3].get_text(" ", strip=True)
        cost = parse_numeric_text(cells[4].get_text(" ", strip=True))
        shares_value = parse_numeric_text(cells[5].get_text(" ", strip=True))
        value_usd = parse_numeric_text(cells[6].get_text(" ", strip=True))
        direction = infer_direction(transaction)
        if date is None or direction is None or OPTIONS_NOISE_PATTERN.search(transaction):
            continue
        shares = int(shares_value or 0)
        value_usd = value_usd or ((cost or 0) * shares)
        if value_usd <= 0:
            continue
        records.append(
            {
                "trade_id": make_trade_id(
                    [date.isoformat(), ticker, owner, relationship or "", transaction, str(cost), str(shares)]
                ),
                "direction": direction,
                "transaction_type": transaction,
                "value_usd": value_usd,
                "weighted_value_usd": value_usd * signal_weight_for_transaction(transaction),
            }
        )
    return records


def resolve_direction_with_company_history(
    trade_details: dict[str, Any] | pd.Series,
    cache: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    trade = trade_details.to_dict() if isinstance(trade_details, pd.Series) else dict(trade_details)
    ticker = trade.get("ticker")
    if not ticker or trade.get("direction") != "sell":
        return trade
    history = cache.get(ticker)
    if history is None:
        history = fetch_company_insider_activity(ticker)
        cache[ticker] = history
    rows = [item for item in history if item.get("trade_id") != trade.get("trade_id")][:COMPANY_HISTORY_ROW_LIMIT]
    buy_strength = sum(item["weighted_value_usd"] for item in rows if item["direction"] == "buy")
    sell_strength = sum(item["weighted_value_usd"] for item in rows if item["direction"] == "sell")
    current_strength = (safe_float(trade.get("value_usd"), 0.0) or 0.0) * signal_weight_for_transaction(
        str(trade.get("transaction_type") or "")
    )
    if (
        buy_strength > sell_strength
        and (sell_strength <= 0 or buy_strength / sell_strength >= COMPANY_TREND_DOMINANCE_RATIO)
        and sell_strength + current_strength < buy_strength
    ):
        trade["direction"] = "buy"
        log.info(
            "Adjusted %s for %s from sell to buy (buy strength %.0f, sell strength %.0f).",
            trade.get("trade_id"),
            ticker,
            buy_strength,
            sell_strength,
        )
    return trade


def classify_options_noise(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    option_groups = {
        (
            str(record.get("ticker") or ""),
            str(record.get("insider") or ""),
            str(record.get("insider_date") or ""),
            int(record.get("shares") or 0),
        )
        for record in records
        if OPTIONS_NOISE_PATTERN.search(str(record.get("transaction_type") or ""))
    }
    for record in records:
        group = (
            str(record.get("ticker") or ""),
            str(record.get("insider") or ""),
            str(record.get("insider_date") or ""),
            int(record.get("shares") or 0),
        )
        transaction = str(record.get("transaction_type") or "")
        if OPTIONS_NOISE_PATTERN.search(transaction):
            record["filter_reason"] = "options_noise"
        elif group in option_groups and record.get("direction") == "sell":
            record["filter_reason"] = "paired_with_option_activity"
    return records


def fetch_insider_trades() -> pd.DataFrame:
    base_url = "https://finviz.com/insidertrading.ashx"
    current_url = base_url
    records: list[dict[str, Any]] = []
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=CONFIG.signal_max_age_hours)).date()
    for page_number in range(1, CONFIG.finviz_max_pages + 1):
        log.info("Fetching recent insider trades from %s", current_url)
        try:
            response = SESSION.get(current_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.error("Failed to fetch Finviz page: %s", exc)
            break
        soup = BeautifulSoup(response.text, "html.parser")
        table = None
        header_map: dict[str, int] = {}
        for candidate in soup.find_all("table"):
            first_row = candidate.find("tr")
            if not first_row:
                continue
            headers = [
                re.sub(r"[^a-zA-Z0-9]", "", cell.get_text(strip=True)).lower()
                for cell in first_row.find_all(["td", "th"])
            ]
            if {"ticker", "transaction", "shares", "date", "cost"}.issubset(headers):
                table = candidate
                header_map = {name: index for index, name in enumerate(headers)}
                break
        if table is None:
            log.error("Finviz insider table was not found; the page layout may have changed.")
            break
        page_dates: list[dt.date] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(header_map.values()):
                continue
            try:
                ticker = cells[header_map["ticker"]].get_text(strip=True).upper()
                transaction = cells[header_map["transaction"]].get_text(strip=True)
                date = parse_finviz_date(cells[header_map["date"]].get_text(strip=True))
                if date is None:
                    continue
                page_dates.append(date)
                if date < cutoff:
                    continue
                cost = parse_numeric_text(cells[header_map["cost"]].get_text(strip=True))
                shares_value = parse_numeric_text(cells[header_map["shares"]].get_text(strip=True))
                if cost in (None, 0) or shares_value is None:
                    continue
                owner_index = header_map.get("owner", header_map.get("insidername"))
                owner = cells[owner_index].get_text(strip=True) if owner_index is not None else "Unknown"
                relationship_index = header_map.get("relationship")
                relationship = (
                    cells[relationship_index].get_text(strip=True) if relationship_index is not None else None
                )
                value_index = header_map.get("value", header_map.get("valueusd"))
                shares = int(shares_value)
                value = parse_numeric_text(cells[value_index].get_text(strip=True)) if value_index is not None else None
                value = value if value is not None else cost * shares
                direction = infer_direction(transaction)
                trade_id = make_trade_id(
                    [date.isoformat(), ticker, owner, relationship or "", transaction, str(cost), str(shares)]
                )
                records.append(
                    {
                        "trade_id": trade_id,
                        "ticker": ticker,
                        "direction": direction,
                        "transaction_type": transaction,
                        "cost": cost,
                        "shares": shares,
                        "value_usd": value,
                        "insider_date": date,
                        "insider": owner,
                        "relationship": relationship,
                        "source_url": current_url,
                        "source_page": page_number,
                        "filter_reason": None,
                    }
                )
            except (ValueError, KeyError, IndexError) as exc:
                log.warning("Skipping malformed Finviz row: %s", exc)
        if page_dates and max(page_dates) < cutoff:
            break
        next_link = soup.find("a", string=lambda text: bool(text and text.strip().lower() == "next"))
        if not next_link or not next_link.get("href"):
            break
        current_url = requests.compat.urljoin(base_url, next_link.get("href"))
        time.sleep(0.5)
    return pd.DataFrame(classify_options_noise(records))


def make_client_order_id(trade_id: str, prefix: str = "insider") -> str:
    digest = hashlib.sha256(trade_id.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def get_broker_context(api: REST) -> dict[str, Any]:
    account = api.get_account()
    positions = list(api.list_positions())
    account_snapshot = {
        "equity": safe_float(object_value(account, "equity")),
        "cash": safe_float(object_value(account, "cash")),
        "buying_power": safe_float(object_value(account, "buying_power")),
        "portfolio_value": safe_float(object_value(account, "portfolio_value")),
        "trading_blocked": bool(object_value(account, "trading_blocked", False)),
        "account_blocked": bool(object_value(account, "account_blocked", False)),
        "trade_suspended_by_user": bool(object_value(account, "trade_suspended_by_user", False)),
    }
    return {
        "account": account,
        "account_snapshot": account_snapshot,
        "positions": positions,
        "positions_by_symbol": {str(object_value(position, "symbol")): position for position in positions},
        "projected_open_positions": len(positions),
        "reserved_notional_usd": 0.0,
    }


def risk_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    gross = sum(
        abs(safe_float(object_value(position, "market_value"), 0.0) or 0.0) for position in context["positions"]
    )
    gross += safe_float(context.get("reserved_notional_usd"), 0.0) or 0.0
    return {
        "open_positions": int(context.get("projected_open_positions", len(context["positions"]))),
        "max_open_positions": CONFIG.max_open_positions,
        "entries_today": STORAGE.count_entries_on_market_date(),
        "max_new_entries_per_day": CONFIG.max_new_entries_per_day,
        "gross_exposure_usd": round(gross, 2),
        "max_gross_exposure_usd": CONFIG.max_gross_exposure_usd,
    }


def check_entry_risk(
    api: REST,
    trade: dict[str, Any],
    capital_usd: float,
    context: dict[str, Any],
) -> tuple[bool, str, Any | None]:
    symbol = str(trade.get("ticker") or "").upper()
    side = trade.get("direction")
    if not symbol or side not in {"buy", "sell"}:
        return False, "invalid symbol or side", None
    if not CONFIG.is_paper_account and not CONFIG.allow_live_trading:
        return False, "live endpoint is not explicitly enabled", None
    account = context["account_snapshot"]
    if account["trading_blocked"] or account["account_blocked"] or account["trade_suspended_by_user"]:
        return False, "Alpaca account is blocked or suspended", None
    if symbol in context["positions_by_symbol"]:
        return False, "position already exists", None
    snapshot = risk_snapshot(context)
    if CONFIG.max_open_positions > 0 and snapshot["open_positions"] >= CONFIG.max_open_positions:
        return False, "maximum open positions reached", None
    if snapshot["entries_today"] >= CONFIG.max_new_entries_per_day:
        return False, "daily entry limit reached", None
    if snapshot["gross_exposure_usd"] + capital_usd > CONFIG.max_gross_exposure_usd:
        return False, "gross exposure limit would be exceeded", None
    buying_power = account.get("buying_power")
    if buying_power is not None and buying_power < capital_usd:
        return False, "insufficient buying power", None
    if side == "sell" and not CONFIG.allow_shorting:
        return False, "shorting is disabled", None
    try:
        asset = api.get_asset(symbol)
    except Exception as exc:
        return False, f"could not verify asset: {exc}", None
    if not bool(object_value(asset, "tradable", False)):
        return False, "asset is not tradable", asset
    if side == "sell":
        if not bool(object_value(asset, "shortable", False)):
            return False, "asset is not shortable", asset
        if not bool(object_value(asset, "easy_to_borrow", False)):
            return False, "asset is not easy to borrow", asset
    return True, "ok", asset


def find_order_by_client_id(api: REST, client_order_id: str) -> Any | None:
    getter = getattr(api, "get_order_by_client_order_id", None)
    if getter is None:
        return None
    try:
        return getter(client_order_id)
    except APIError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 404 or "not found" in str(exc).lower():
            return None
        raise


def wait_for_order(api: REST, order: Any) -> Any:
    deadline = time.monotonic() + CONFIG.order_fill_timeout_seconds
    current = order
    while str(object_value(current, "status", "")) not in ORDER_TERMINAL_STATUSES and time.monotonic() < deadline:
        time.sleep(1)
        current = api.get_order(str(object_value(current, "id")))
    return current


def place_entry_order(
    api: REST,
    trade: dict[str, Any],
    capital_usd: float,
    context: dict[str, Any],
) -> tuple[bool, str]:
    trade_id = str(trade.get("trade_id") or "")
    symbol = str(trade.get("ticker") or "").upper()
    side = str(trade.get("direction") or "")
    allowed, reason, asset = check_entry_risk(api, trade, capital_usd, context)
    if not allowed:
        return False, reason
    if CONFIG.dry_run:
        STORAGE.update_signal_status(trade_id, "dry_run", f"would submit {side} ${capital_usd:.2f} of {symbol}")
        STORAGE.mark_trades_seen([trade_id])
        log.info("DRY RUN: would submit %s entry for %s with $%.2f", side, symbol, capital_usd)
        return True, "dry run"
    try:
        latest_price = safe_float(api.get_latest_trade(symbol).price)
        if latest_price is None or latest_price <= 0:
            return False, "latest price was invalid"
        if side == "sell" or not bool(object_value(asset, "fractionable", False)):
            qty: float | int = int(capital_usd / latest_price)
            if qty < 1:
                return False, "capital is too low for one whole share"
        else:
            qty = round(capital_usd / latest_price, 4)
            if qty < 0.0001:
                return False, "calculated fractional quantity is too small"
        if side == "sell":
            tp_price = round(latest_price * (1 - CONFIG.take_profit_percent / 100), 2)
            sl_price = round(latest_price * (1 + CONFIG.stop_loss_percent / 100), 2)
        else:
            tp_price = round(latest_price * (1 + CONFIG.take_profit_percent / 100), 2)
            sl_price = round(latest_price * (1 - CONFIG.stop_loss_percent / 100), 2)
        client_order_id = make_client_order_id(trade_id)
        order = find_order_by_client_id(api, client_order_id)
        if order is None:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="day",
                client_order_id=client_order_id,
            )
        STORAGE.record_trade_submission(trade, order, latest_price, tp_price, sl_price)
        final_order = wait_for_order(api, order)
        STORAGE.update_entry_order(
            final_order,
            take_profit_percent=CONFIG.take_profit_percent,
            stop_loss_percent=CONFIG.stop_loss_percent,
        )
        status = str(object_value(final_order, "status", "unknown"))
        if status in {"rejected", "canceled", "expired"}:
            return False, f"entry order ended as {status}"
        return True, status
    except Exception as exc:
        log.error("Failed to place entry for %s: %s", symbol, exc)
        return False, str(exc)


def reconcile_orders(api: REST) -> tuple[int, int]:
    entry_count = 0
    exit_count = 0
    for row in STORAGE.get_unreconciled_entries():
        try:
            order = api.get_order(row["alpaca_order_id"])
            STORAGE.update_entry_order(
                order,
                take_profit_percent=CONFIG.take_profit_percent,
                stop_loss_percent=CONFIG.stop_loss_percent,
            )
            entry_count += 1
        except Exception as exc:
            log.warning("Could not reconcile entry order %s: %s", row["alpaca_order_id"], exc)
    for row in STORAGE.get_unreconciled_exits():
        try:
            order = api.get_order(row["exit_order_id"])
            STORAGE.update_exit_order(order)
            exit_count += 1
        except Exception as exc:
            log.warning("Could not reconcile exit order %s: %s", row["exit_order_id"], exc)
    return entry_count, exit_count


def submit_managed_exit(
    api: REST,
    trade: dict[str, Any],
    reason: str,
    position: Any,
) -> tuple[bool, str]:
    history_id = int(trade["id"])
    symbol = str(trade["ticker"])
    if CONFIG.dry_run:
        log.info("DRY RUN: would exit managed %s position because %s", symbol, reason)
        return True, "dry run"
    entry_side = str(trade["side"])
    exit_side = "buy" if entry_side == "sell" else "sell"
    bot_qty = abs(safe_float(trade.get("filled_qty") or trade.get("order_qty"), 0.0) or 0.0)
    position_qty = abs(safe_float(object_value(position, "qty"), 0.0) or 0.0)
    qty = min(bot_qty, position_qty)
    if qty <= 0:
        return False, "managed quantity is unavailable"
    client_order_id = make_client_order_id(str(history_id), prefix="insider-exit")
    try:
        order = find_order_by_client_id(api, client_order_id)
        if order is None:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side=exit_side,
                type="market",
                time_in_force="day",
                client_order_id=client_order_id,
            )
        STORAGE.record_exit_submission(history_id, order, reason)
        final_order = wait_for_order(api, order)
        STORAGE.update_exit_order(final_order)
        status = str(object_value(final_order, "status", "unknown"))
        return status not in {"rejected", "canceled", "expired"}, status
    except Exception as exc:
        return False, str(exc)


def queue_pending_trade(trade: dict[str, Any]) -> bool:
    return STORAGE.queue_entry(trade, expiry_hours=CONFIG.queue_expiry_hours)


def queue_pending_exit(trade: dict[str, Any], reason: str) -> bool:
    exit_side = "buy" if trade.get("side") == "sell" else "sell"
    return STORAGE.queue_exit(
        str(trade["ticker"]),
        reason,
        trade_history_id=int(trade["id"]),
        qty=safe_float(trade.get("filled_qty") or trade.get("order_qty")),
        side=exit_side,
        expiry_hours=CONFIG.queue_expiry_hours,
    )


def execute_pending_orders(
    api: REST,
    capital_per_trade_usd: float | None,
    context: dict[str, Any],
) -> bool:
    STORAGE.expire_stale_queue()
    pending = STORAGE.load_pending_orders(due_only=True)
    modified = False
    for queued in pending["sell"]:
        queue_id = int(queued["queue_id"])
        history_id = queued.get("trade_history_id")
        trade = STORAGE.get_trade_by_id(int(history_id)) if history_id is not None else None
        if trade is None:
            STORAGE.record_queue_attempt(queue_id)
            STORAGE.record_queue_failure(
                queue_id,
                "managed trade no longer exists",
                max_attempts=CONFIG.max_queue_attempts,
                retry_base_seconds=CONFIG.queue_retry_base_seconds,
            )
            continue
        position = context["positions_by_symbol"].get(str(trade["ticker"]))
        STORAGE.record_queue_attempt(queue_id)
        if position is None:
            STORAGE.record_queue_failure(
                queue_id,
                "broker position not found",
                max_attempts=CONFIG.max_queue_attempts,
                retry_base_seconds=CONFIG.queue_retry_base_seconds,
            )
            continue
        success, note = submit_managed_exit(api, trade, str(queued.get("reason") or "queued exit"), position)
        if success:
            STORAGE.mark_queue_executed(queue_id)
            modified = True
        else:
            STORAGE.record_queue_failure(
                queue_id,
                note,
                max_attempts=CONFIG.max_queue_attempts,
                retry_base_seconds=CONFIG.queue_retry_base_seconds,
            )

    if capital_per_trade_usd is None:
        return modified
    for trade in pending["buy"]:
        queue_id = int(trade["queue_id"])
        STORAGE.record_queue_attempt(queue_id)
        success, note = place_entry_order(api, dict(trade), capital_per_trade_usd, context)
        if success:
            STORAGE.mark_queue_executed(queue_id)
            if trade.get("ticker") not in context["positions_by_symbol"]:
                context["positions_by_symbol"][trade["ticker"]] = object()
                context["projected_open_positions"] += 1
                context["reserved_notional_usd"] += capital_per_trade_usd
            modified = True
        else:
            status = STORAGE.record_queue_failure(
                queue_id,
                note,
                max_attempts=CONFIG.max_queue_attempts,
                retry_base_seconds=CONFIG.queue_retry_base_seconds,
            )
            if status != "pending":
                STORAGE.update_signal_status(trade.get("trade_id"), "failed", note)
    refresh_runtime_views(context)
    return modified


def process_insider_trades(
    api: REST,
    is_market_open: bool,
    capital_per_trade_usd: float | None,
    context: dict[str, Any],
) -> bool:
    latest = fetch_insider_trades()
    RUNTIME_STATE.set_latest_scrape_rows(len(latest.index))
    heartbeat("scrape", note=f"rows={len(latest)}")
    if latest.empty:
        return False

    if STORAGE.get_meta("signal_baseline_complete") != "true":
        if STORAGE.count_seen_trades() == 0:
            baseline_ids: list[str] = []
            for _, row in latest.iterrows():
                trade = row.to_dict()
                STORAGE.upsert_signal(trade, status="baseline", note="first-run safety baseline")
                baseline_ids.append(trade["trade_id"])
            STORAGE.mark_trades_seen(baseline_ids)
            STORAGE.set_meta("signal_baseline_complete", "true")
            log.warning("First-run baseline recorded %s signals without trading them.", len(baseline_ids))
            heartbeat("baseline", note=f"recorded={len(baseline_ids)}")
            return True
        STORAGE.set_meta("signal_baseline_complete", "true")

    seen = STORAGE.load_seen_trade_ids()
    unseen = latest[~latest["trade_id"].isin(seen)]
    if unseen.empty:
        return False
    cache: dict[str, list[dict[str, Any]]] = {}
    pending = STORAGE.load_pending_orders()
    pending_symbols = {str(item.get("ticker")) for item in pending["buy"]}
    positions = set(context["positions_by_symbol"])
    entry_slots = max(
        0,
        CONFIG.max_new_entries_per_day - STORAGE.count_entries_on_market_date() - len(pending["buy"]),
    )
    changed = False
    for _, row in unseen.iterrows():
        trade = row.to_dict()
        if not trade.get("filter_reason"):
            trade = resolve_direction_with_company_history(trade, cache)
        trade_id = trade["trade_id"]
        symbol = trade["ticker"]
        STORAGE.upsert_signal(trade)
        if trade.get("filter_reason"):
            STORAGE.update_signal_status(trade_id, "filtered", trade["filter_reason"])
            STORAGE.mark_trades_seen([trade_id])
            continue
        if trade.get("direction") not in {"buy", "sell"}:
            STORAGE.update_signal_status(trade_id, "filtered", "unsupported transaction type")
            STORAGE.mark_trades_seen([trade_id])
            continue
        if trade["direction"] == "sell" and not CONFIG.allow_shorting:
            STORAGE.update_signal_status(trade_id, "filtered", "shorting disabled")
            STORAGE.mark_trades_seen([trade_id])
            continue
        if symbol in positions or symbol in pending_symbols:
            STORAGE.update_signal_status(trade_id, "skipped", "position or entry already exists")
            STORAGE.mark_trades_seen([trade_id])
            continue
        if entry_slots <= 0:
            STORAGE.update_signal_status(trade_id, "deferred", "daily entry slots exhausted")
            continue
        if is_market_open and capital_per_trade_usd is not None:
            success, note = place_entry_order(api, trade, capital_per_trade_usd, context)
            if success:
                positions.add(symbol)
                if symbol not in context["positions_by_symbol"]:
                    context["positions_by_symbol"][symbol] = object()
                    context["projected_open_positions"] += 1
                    context["reserved_notional_usd"] += capital_per_trade_usd
                entry_slots -= 1
                changed = True
                heartbeat("order", note=f"entry:{symbol}:{note}")
            elif note in {
                "maximum open positions reached",
                "daily entry limit reached",
                "gross exposure limit would be exceeded",
            }:
                STORAGE.update_signal_status(trade_id, "deferred", note)
                continue
            else:
                STORAGE.update_signal_status(trade_id, "observed", note)
        else:
            if queue_pending_trade(trade):
                pending_symbols.add(symbol)
                entry_slots -= 1
                changed = True
    refresh_runtime_views(context)
    return changed


def manage_positions(api: REST, context: dict[str, Any]) -> bool:
    changed = False
    now = dt.datetime.now(dt.timezone.utc)
    for trade in STORAGE.get_managed_open_trades():
        symbol = str(trade["ticker"])
        position = context["positions_by_symbol"].get(symbol)
        if position is None:
            log.warning("Managed trade %s has no matching broker position; no exit was submitted.", trade["id"])
            continue
        if trade.get("exit_order_id") and str(trade.get("exit_order_status") or "") not in ORDER_TERMINAL_STATUSES:
            continue
        try:
            current_price = safe_float(api.get_latest_trade(symbol).price)
            if current_price is None:
                continue
            tp_price = safe_float(trade.get("take_profit_price"))
            sl_price = safe_float(trade.get("stop_loss_price"))
            side = str(trade.get("side"))
            reason = None
            if side == "sell":
                if tp_price is not None and current_price <= tp_price:
                    reason = f"take profit at ${tp_price:.2f}"
                elif sl_price is not None and current_price >= sl_price:
                    reason = f"stop loss at ${sl_price:.2f}"
            else:
                if tp_price is not None and current_price >= tp_price:
                    reason = f"take profit at ${tp_price:.2f}"
                elif sl_price is not None and current_price <= sl_price:
                    reason = f"stop loss at ${sl_price:.2f}"
            filled_at = normalize_utc_datetime(
                dt.datetime.fromisoformat(str(trade["filled_at_utc"]).replace("Z", "+00:00"))
            )
            if reason is None and filled_at is not None and now - filled_at >= dt.timedelta(days=CONFIG.max_hold_days):
                reason = f"maximum hold of {CONFIG.max_hold_days} days"
            if reason:
                success, note = submit_managed_exit(api, trade, reason, position)
                if success:
                    changed = True
                    heartbeat("order", note=f"exit:{symbol}:{note}")
                elif queue_pending_exit(trade, reason):
                    changed = True
                    heartbeat("order", ok=False, note=f"queued_exit:{symbol}:{note}")
        except Exception as exc:
            log.error("Error managing bot-owned position %s: %s", symbol, exc)
    return changed


def fetch_performance_snapshot() -> dict[str, Any]:
    headers = {
        "APCA-API-KEY-ID": CONFIG.api_key,
        "APCA-API-SECRET-KEY": CONFIG.secret_key,
    }
    trading_url = f"{CONFIG.base_url}/v2/account/portfolio/history"
    response = SESSION.get(
        trading_url,
        headers=headers,
        params={
            "period": f"{CONFIG.performance_lookback_days}D",
            "timeframe": "1D",
            "intraday_reporting": "market_hours",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    history = response.json()
    account_by_date: dict[str, float] = {}
    for timestamp, equity in zip(history.get("timestamp") or [], history.get("equity") or []):
        value = safe_float(equity)
        if value is None or value <= 0:
            continue
        date = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc).date().isoformat()
        account_by_date[date] = value
    if len(account_by_date) < 2:
        raise ValueError("Alpaca portfolio history returned fewer than two usable points")
    start_date = min(account_by_date)
    bars_response = SESSION.get(
        f"https://data.alpaca.markets/v2/stocks/{CONFIG.benchmark_symbol}/bars",
        headers=headers,
        params={
            "timeframe": "1Day",
            "start": start_date,
            "limit": 1000,
            "adjustment": "all",
            "feed": "iex",
            "sort": "asc",
        },
        timeout=REQUEST_TIMEOUT,
    )
    bars_response.raise_for_status()
    benchmark_by_date: dict[str, float] = {}
    for bar in bars_response.json().get("bars") or []:
        close = safe_float(bar.get("c"))
        if close is not None and close > 0 and bar.get("t"):
            benchmark_by_date[str(bar["t"])[:10]] = close
    common_dates = sorted(set(account_by_date) & set(benchmark_by_date))
    if len(common_dates) < 2:
        raise ValueError("benchmark history did not overlap account history")
    first_date = common_dates[0]
    base_equity = account_by_date[first_date]
    base_benchmark = benchmark_by_date[first_date]
    points = [
        {
            "date": date,
            "account_return_pct": round((account_by_date[date] / base_equity - 1) * 100, 3),
            "benchmark_return_pct": round((benchmark_by_date[date] / base_benchmark - 1) * 100, 3),
        }
        for date in common_dates
    ]
    account_return = points[-1]["account_return_pct"]
    benchmark_return = points[-1]["benchmark_return_pct"]
    return {
        "benchmark_symbol": CONFIG.benchmark_symbol,
        "account_return_pct": account_return,
        "benchmark_return_pct": benchmark_return,
        "alpha_pct": round(account_return - benchmark_return, 3),
        "lookback_label": f"{first_date} to {common_dates[-1]}",
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "points": points,
        "trade_summary": STORAGE.get_performance_summary(),
        "scope_note": "Alpaca account equity versus a buy-and-hold benchmark; other activity in the account is included.",
    }


def initialize_runtime() -> REST:
    STORAGE.initialize()
    heartbeat("storage", note="sqlite initialized")
    RUNTIME_STATE.set_mode(
        {
            "dry_run": CONFIG.dry_run,
            "paper_account": CONFIG.is_paper_account,
            "shorting_enabled": CONFIG.allow_shorting,
            "live_trading_enabled": CONFIG.allow_live_trading,
        }
    )
    refresh_runtime_views()
    if CONFIG.monitoring_enabled:
        start_monitoring_server(
            RUNTIME_STATE,
            host=CONFIG.monitoring_host,
            port=CONFIG.monitoring_port,
            log=log,
            token=CONFIG.monitoring_token,
        )
        log.info("Monitoring server listening on http://%s:%s", CONFIG.monitoring_host, CONFIG.monitoring_port)
    api = REST(
        key_id=CONFIG.api_key,
        secret_key=CONFIG.secret_key,
        base_url=CONFIG.base_url,
        api_version="v2",
    )
    try:
        original_request = api._session.request
        api._session.request = functools.partial(original_request, timeout=REQUEST_TIMEOUT)
    except Exception:
        log.warning("Could not attach a timeout to the Alpaca SDK session.")
    return api


def run() -> None:
    api = initialize_runtime()
    log.info(
        "Starting Insider Edge (dry_run=%s, paper=%s, shorting=%s)",
        CONFIG.dry_run,
        CONFIG.is_paper_account,
        CONFIG.allow_shorting,
    )
    last_scan = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    last_position_check = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    last_performance_refresh = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    while True:
        cycle_start = dt.datetime.now(dt.timezone.utc)
        heartbeat("loop_start")
        is_market_open = False
        next_open_at: dt.datetime | None = None
        clock_available = False
        context: dict[str, Any] | None = None
        try:
            try:
                clock = api.get_clock()
                is_market_open = bool(clock.is_open)
                next_open_at = normalize_utc_datetime(getattr(clock, "next_open", None))
                clock_available = True
                RUNTIME_STATE.set_market_open(is_market_open)
                heartbeat("alpaca_clock", note="open" if is_market_open else "closed")
                context = get_broker_context(api)
                heartbeat("broker_state", note=f"positions={len(context['positions'])}")
            except Exception as exc:
                RUNTIME_STATE.set_market_open(None)
                heartbeat("alpaca_clock", ok=False, note=str(exc))
                log.error("Broker state is unavailable; entries and exits are paused: %s", exc)

            if context is not None:
                entries, exits = reconcile_orders(api)
                heartbeat("reconcile", note=f"entries={entries},exits={exits}")

            capital_usd = None
            if is_market_open and context is not None:
                rate = get_usd_per_czk()
                if rate is not None:
                    capital_usd = CONFIG.trade_capital_czk * rate
                    heartbeat("fx", note=f"capital_usd={capital_usd:.2f}")
                else:
                    heartbeat("fx", ok=False, note="rate unavailable")

            if is_market_open and context is not None:
                try:
                    execute_pending_orders(api, capital_usd, context)
                    heartbeat("pending_orders", note="processed due queue")
                except Exception as exc:
                    heartbeat("pending_orders", ok=False, note=str(exc))
                    log.error("Queued-order processing failed: %s", exc)

            if (
                clock_available
                and context is not None
                and cycle_start - last_scan >= dt.timedelta(minutes=CONFIG.insider_scan_interval_minutes)
            ):
                try:
                    process_insider_trades(api, is_market_open, capital_usd, context)
                except Exception as exc:
                    heartbeat("scrape", ok=False, note=str(exc))
                    log.error("Insider scan failed: %s", exc, exc_info=True)
                finally:
                    last_scan = cycle_start

            if (
                is_market_open
                and context is not None
                and cycle_start - last_position_check >= dt.timedelta(minutes=CONFIG.position_check_interval_minutes)
            ):
                try:
                    manage_positions(api, context)
                    heartbeat("manage_positions", note="checked bot-owned positions")
                except Exception as exc:
                    heartbeat("manage_positions", ok=False, note=str(exc))
                    log.error("Position management failed: %s", exc, exc_info=True)
                finally:
                    last_position_check = cycle_start

            if context is not None and cycle_start - last_performance_refresh >= dt.timedelta(
                minutes=CONFIG.performance_refresh_minutes
            ):
                try:
                    RUNTIME_STATE.set_performance(fetch_performance_snapshot())
                    heartbeat("performance", note=f"benchmark={CONFIG.benchmark_symbol}")
                except Exception as exc:
                    heartbeat("performance", ok=False, note=str(exc))
                    log.warning("Performance refresh failed: %s", exc)
                finally:
                    last_performance_refresh = cycle_start

            refresh_runtime_views(context)
            heartbeat("storage", note="runtime views refreshed")
        except Exception as exc:
            heartbeat("loop_exception", ok=False, note=str(exc))
            log.critical("Unhandled main-loop error: %s", exc, exc_info=True)

        pending = STORAGE.load_pending_orders()
        sleep_seconds = compute_sleep_seconds(
            is_market_open=is_market_open,
            pending_orders=pending,
            next_open_at=next_open_at,
        )
        heartbeat("sleep", note=f"{sleep_seconds}s")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    run()
