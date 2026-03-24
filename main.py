import datetime
import functools
import hashlib
import json
import logging
import os
import re
import sys
import time

import pandas as pd
import requests
from alpaca_trade_api.rest import REST, APIError
from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import load_config
from monitoring import RuntimeState, start_monitoring_server
from storage import Storage


try:
    CONFIG = load_config()
except ValueError as exc:
    raise SystemExit(str(exc)) from exc

# --- Basic Setup ---
REQUEST_TIMEOUT = CONFIG.request_timeout
HEARTBEAT_FILE = CONFIG.heartbeat_file
APP_LOG_FILE = CONFIG.app_log_file
SQLITE_DB_PATH = CONFIG.sqlite_db_path

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(console_handler)

# Rotating file handler (~5 MB total across 5 files)
file_handler = RotatingFileHandler(APP_LOG_FILE, maxBytes=1_000_000, backupCount=5)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
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
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    return session


SESSION = make_session()


def heartbeat(stage: str, ok: bool = True, note: str = ""):
    try:
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        status = "OK" if ok else "ERR"
        line = f"{timestamp}\t{stage}\t{status}\t{note}\n"
        with open(HEARTBEAT_FILE, "a", encoding="utf-8") as hb:
            hb.write(line)
            hb.flush()
            os.fsync(hb.fileno())
    except Exception as exc:
        log.error(f"Failed to write heartbeat: {exc}")
    finally:
        RUNTIME_STATE.record_heartbeat(stage, ok=ok, note=note)

# --- ALPACA API CONFIGURATION ---
API_KEY = CONFIG.api_key
SECRET_KEY = CONFIG.secret_key
BASE_URL = CONFIG.base_url

# --- State Management & Parameters ---
TRADE_HISTORY_CSV = CONFIG.trade_history_csv
SEEN_TRADES_LOG = CONFIG.seen_trades_log
PENDING_ORDERS_JSON = CONFIG.pending_orders_json
STATE_DB_PATH = CONFIG.sqlite_db_path

TRADE_CAPITAL_CZK = CONFIG.trade_capital_czk
TAKE_PROFIT_PERCENT = CONFIG.take_profit_percent
INSIDER_SCAN_INTERVAL_MINUTES = CONFIG.insider_scan_interval_minutes
POSITION_CHECK_INTERVAL_MINUTES = CONFIG.position_check_interval_minutes
MARKET_OPEN_POLL_SECONDS = CONFIG.market_open_poll_seconds
MARKET_CLOSED_POLL_SECONDS = CONFIG.market_closed_poll_seconds

OPTIONS_NOISE_PATTERN = re.compile(
    r"(option|exercise|derivative|convert|conversion|grant|award)",
    re.IGNORECASE,
)
STORAGE = Storage(
    db_path=STATE_DB_PATH,
    trade_history_csv=TRADE_HISTORY_CSV,
    seen_trades_log=SEEN_TRADES_LOG,
    pending_orders_json=PENDING_ORDERS_JSON,
    log=log,
)


def refresh_runtime_views() -> None:
    RUNTIME_STATE.update_pending_orders(STORAGE.get_pending_summary())
    RUNTIME_STATE.set_trade_history_rows(STORAGE.count_trade_history())
    RUNTIME_STATE.set_queue_preview(STORAGE.get_queue_preview())
    RUNTIME_STATE.set_insider_leaderboard(STORAGE.get_insider_leaderboard())


def get_usd_per_czk() -> float | None:
    try:
        url = "https://api.exchangerate-api.com/v4/latest/CZK"
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        rate = data['rates']['USD']
        log.info(f"Fetched CZK to USD exchange rate: {rate}")
        return float(rate)
    except Exception as e:
        log.error(f"Could not fetch CZK to USD exchange rate: {e}")
        return None


def effective_insider_scan_interval_minutes(is_market_open: bool) -> int:
    target = 2 if is_market_open else 5
    return max(1, min(INSIDER_SCAN_INTERVAL_MINUTES, target))


def effective_position_check_interval_minutes() -> int:
    return max(1, min(POSITION_CHECK_INTERVAL_MINUTES, 2))


def normalize_utc_datetime(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def compute_sleep_seconds(
    *,
    is_market_open: bool,
    pending_orders: dict,
    next_open_at: datetime.datetime | None,
) -> int:
    if is_market_open:
        return max(15, min(MARKET_OPEN_POLL_SECONDS, 30))

    has_pending = bool((pending_orders.get("buy") or []) or (pending_orders.get("sell") or []))
    if has_pending:
        normalized_next_open = normalize_utc_datetime(next_open_at)
        if normalized_next_open is not None:
            seconds_until_open = max(15, int((normalized_next_open - datetime.datetime.utcnow()).total_seconds()))
            return min(seconds_until_open, 60)
        return max(15, min(MARKET_CLOSED_POLL_SECONDS, 60))

    return max(60, min(MARKET_CLOSED_POLL_SECONDS, 300))

def parse_finviz_date(raw: str) -> datetime.date | None:
    # This function is from your script and remains unchanged.
    try: return datetime.datetime.strptime(raw.strip(), "%b %d '%y").date()
    except ValueError:
        try:
            dt = datetime.datetime.strptime(raw.strip(), "%b %d").date()
            return dt.replace(year=datetime.date.today().year)
        except Exception: return None

def infer_direction(txn: str) -> str | None:
    lower = txn.lower()
    if 'buy' in lower: return 'buy'
    if 'sell' in lower or 'sale' in lower: return 'sell'
    return None


def classify_options_noise(records: list[dict]) -> list[dict]:
    option_groups: set[tuple[str, str, str, int]] = set()
    for record in records:
        transaction_type = str(record.get("transaction_type") or "")
        if OPTIONS_NOISE_PATTERN.search(transaction_type):
            option_groups.add(
                (
                    str(record.get("ticker") or ""),
                    str(record.get("insider") or ""),
                    str(record.get("insider_date") or ""),
                    int(record.get("shares") or 0),
                )
            )

    for record in records:
        filter_reason = record.get("filter_reason")
        transaction_type = str(record.get("transaction_type") or "")
        group_key = (
            str(record.get("ticker") or ""),
            str(record.get("insider") or ""),
            str(record.get("insider_date") or ""),
            int(record.get("shares") or 0),
        )
        if OPTIONS_NOISE_PATTERN.search(transaction_type):
            record["filter_reason"] = "options_noise"
        elif (
            group_key in option_groups
            and str(record.get("direction")) == "sell"
        ):
            record["filter_reason"] = "paired_with_option_activity"
        else:
            record["filter_reason"] = filter_reason
    return records


def make_trade_id(parts: list[str]) -> str:
    raw = "|".join(parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{parts[0]}-{parts[1]}-{digest}"

# --------------------------------------------------
# 1. Scrape insider transactions from Finviz
# --------------------------------------------------
def fetch_insider_trades() -> pd.DataFrame:
    base_url = 'https://finviz.com/insidertrading.ashx'
    all_records = []
    current_page = 1
    current_url = base_url
    while True:
        log.info(f"Fetching insider trades from: {current_url}")
        try:
            resp = SESSION.get(current_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"Failed to fetch Finviz page: {e}")
            break

        soup = BeautifulSoup(resp.text, 'html.parser')
        target_table = None
        header_map = {}
        for table in soup.find_all('table'):
            first_row = table.find('tr')
            if not first_row:
                continue
            header_cells = [cell.get_text(strip=True) for cell in first_row.find_all(['td', 'th'])]
            normalized_headers = [re.sub(r'[^a-zA-Z0-9]', '', h).lower() for h in header_cells]
            if 'ticker' in normalized_headers and 'transaction' in normalized_headers and 'shares' in normalized_headers:
                header_map = {name: i for i, name in enumerate(normalized_headers)}
                target_table = table
                break

        if not target_table:
            log.error("Could not find a valid insider trading table on the page.")
            break

        rows = target_table.find_all('tr')[1:]
        if not rows:
            log.info("No more trade rows found on this page. Ending scrape.")
            break

        for tr in rows:
            tds = tr.find_all('td')
            if len(tds) < len(header_map):
                continue
            try:
                ticker = tds[header_map['ticker']].text.strip()
                transaction = tds[header_map['transaction']].text.strip()
                date_raw = tds[header_map['date']].text.strip()
                cost_raw = tds[header_map['cost']].text.strip()
                shares_raw = tds[header_map['shares']].text.strip()
                owner = tds[header_map.get('owner', header_map.get('insidername', -1))].text.strip()
                relationship_idx = header_map.get('relationship', -1)
                relationship = tds[relationship_idx].text.strip() if relationship_idx >= 0 else None
                value_idx = header_map.get('value', header_map.get('valueusd', -1))
                value_raw = tds[value_idx].text.strip() if value_idx >= 0 else None
                direction = infer_direction(transaction)
                cost = float(cost_raw.replace(',', ''))
                shares = int(shares_raw.replace(',', ''))
                date = parse_finviz_date(date_raw)
                if cost == 0 or not date:
                    continue
                value_usd = None
                if value_raw:
                    value_usd = float(value_raw.replace(',', '').replace('$', ''))
                if value_usd is None:
                    value_usd = float(cost * shares)
                trade_id = make_trade_id(
                    [
                        date.isoformat(),
                        ticker,
                        owner,
                        relationship or "",
                        transaction,
                        str(cost),
                        str(shares),
                    ]
                )
                all_records.append(
                    {
                        'trade_id': trade_id,
                        'ticker': ticker,
                        'direction': direction,
                        'transaction_type': transaction,
                        'cost': cost,
                        'shares': shares,
                        'value_usd': value_usd,
                        'insider_date': date,
                        'insider': owner,
                        'relationship': relationship,
                        'source_url': current_url,
                        'source_page': current_page,
                        'filter_reason': None,
                    }
                )
            except (ValueError, KeyError, IndexError) as e:
                log.warning(f"Skipping a row due to parsing error: {e}")
                continue

        next_link = soup.find('a', string='next')
        if next_link and next_link.get('href'):
            current_url = 'https://finviz.com/' + next_link.get('href')
            current_page += 1
            time.sleep(1)
        else:
            log.info("No 'next' page link found. Scrape complete.")
            break

    all_records = classify_options_noise(all_records)
    return pd.DataFrame(all_records)

# --------------------------------------------------
# 2. Live Trading Logic and Persistent Memory
# --------------------------------------------------


def refresh_trade_history_metrics() -> None:
    try:
        STORAGE.export_legacy_files()
        refresh_runtime_views()
    except Exception as exc:
        log.warning(f"Could not refresh storage-backed metrics: {exc}")


def load_seen_trade_ids() -> set:
    return STORAGE.load_seen_trade_ids()

def log_trades_as_seen(new_trade_ids: list):
    for trade_id in new_trade_ids:
        STORAGE.mark_trade_seen(trade_id)
    log.info(f"Logged {len(new_trade_ids)} new trades to SQLite seen state.")

def load_trade_history() -> pd.DataFrame:
    df = STORAGE.get_trade_history_df()
    RUNTIME_STATE.set_trade_history_rows(len(df.index))
    return df

def log_trade_to_history(
    trade_details,
    order_obj,
    entry_price: float,
    tp_price: float,
    sl_price: float | None = None,
):
    if isinstance(trade_details, pd.Series):
        trade_info = trade_details.to_dict()
    else:
        trade_info = dict(trade_details)

    insider_date = trade_info.get('insider_date')
    if isinstance(insider_date, (datetime.date, datetime.datetime)):
        insider_date_str = insider_date.isoformat()
    elif insider_date is not None:
        insider_date_str = str(insider_date)
    else:
        insider_date_str = None

    record = {
        'trade_id': trade_info.get('trade_id'), 'timestamp_utc': datetime.datetime.utcnow().isoformat(),
        'ticker': order_obj.symbol, 'side': order_obj.side, 'order_qty': order_obj.qty,
        'insider_date': insider_date_str, 'insider_name': trade_info.get('insider'),
        'estimated_entry_price': entry_price, 'take_profit_price': tp_price, 'stop_loss_price': sl_price,
        'alpaca_order_id': order_obj.id, 'status': order_obj.status, 'exit_reason': None,
        'exit_timestamp_utc': None
    }
    STORAGE.record_trade_execution(trade_info, order_obj, entry_price, tp_price, sl_price)
    log.info(f"Successfully logged trade execution {record['trade_id']} to SQLite trade history.")
    refresh_runtime_views()


def update_trade_exit_in_history(symbol: str, exit_reason: str, exit_price: float | None = None):
    STORAGE.update_trade_exit(symbol, exit_reason, exit_price)
    log.info(f"Updated trade history for {symbol} with exit reason '{exit_reason}'.")
    refresh_runtime_views()


def ensure_pending_structure(data: dict | None) -> dict:
    if not isinstance(data, dict):
        data = {}
    data.setdefault('buy', [])
    data.setdefault('sell', [])
    return data


def load_pending_orders() -> dict:
    pending = ensure_pending_structure(STORAGE.load_pending_orders())
    RUNTIME_STATE.update_pending_orders(STORAGE.get_pending_summary())
    return pending


def save_pending_orders(pending_orders: dict):
    STORAGE.export_legacy_files()
    log.info(f"Persisted pending orders queue to {STATE_DB_PATH}.")
    refresh_runtime_views()


def queue_pending_trade(pending_orders: dict, trade_details) -> bool:
    trade_info = trade_details.to_dict() if isinstance(trade_details, pd.Series) else dict(trade_details)
    trade_id = trade_info.get('trade_id')
    ticker = trade_info.get('ticker')
    if not trade_id or not ticker:
        log.warning("Cannot queue trade without trade_id and ticker.")
        return False

    if not STORAGE.queue_entry(trade_info):
        log.info(f"Trade {trade_id} for {ticker} is already queued for execution.")
        return False

    log.info(
        "Queued %s order for %s (%s) from %s [%s | %s] until market hours.",
        trade_info.get('direction', 'buy'),
        ticker,
        trade_id,
        trade_info.get('insider') or 'unknown insider',
        trade_info.get('relationship') or 'unknown relationship',
        trade_info.get('transaction_type') or 'unknown transaction',
    )
    refresh_runtime_views()
    return True


def queue_pending_sell(pending_orders: dict, symbol: str, reason: str) -> bool:
    if not symbol:
        log.warning("Cannot queue sell order without a symbol.")
        return False

    if not STORAGE.queue_exit(symbol, reason):
        log.info(f"Sell order for {symbol} ({reason}) is already queued.")
        return False

    log.info(f"Queued sell order for {symbol} due to {reason}.")
    refresh_runtime_views()
    return True


def execute_pending_orders(
    api: REST,
    pending_orders: dict,
    capital_per_trade_usd: float | None,
) -> bool:
    pending = ensure_pending_structure(pending_orders)
    modified = False

    sell_orders = pending.get('sell', [])
    if sell_orders:
        for order in sell_orders:
            queue_id = order.get("queue_id")
            symbol = order.get('symbol')
            reason = order.get('reason', 'pending sell')
            if not symbol:
                continue
            if queue_id:
                STORAGE.record_queue_attempt(int(queue_id))
            try:
                exit_price = api.get_latest_trade(symbol).price
                api.close_position(symbol)
                log.info(f"Executed queued sell for {symbol} ({reason}).")
                update_trade_exit_in_history(symbol, reason, exit_price)
                if queue_id:
                    STORAGE.mark_queue_executed(int(queue_id))
                heartbeat("order", note=f"sell:{symbol}")
                modified = True
            except Exception as e:
                log.error(f"Failed to execute queued sell for {symbol}: {e}")

    buy_orders = pending.get('buy', [])
    if buy_orders:
        if capital_per_trade_usd is None:
            log.error("Cannot execute queued buy orders without capital information. Will retry later.")
            return modified
        try:
            open_positions = {p.symbol for p in api.list_positions()}
        except Exception as e:
            log.error(f"Could not refresh open positions before executing queued buys: {e}")
            open_positions = set()

        for order in buy_orders:
            trade = dict(order)
            queue_id = trade.get("queue_id")
            symbol = trade.get('ticker')
            if not symbol:
                log.warning("Skipping queued buy order without a ticker symbol.")
                continue
            if symbol in open_positions:
                log.info(f"Skipping queued buy for {symbol}: position already open.")
                STORAGE.update_signal_status(trade.get("trade_id"), "skipped", "position already open during queued execution")
                if trade.get("trade_id"):
                    STORAGE.mark_trade_seen(trade["trade_id"])
                if queue_id:
                    STORAGE.mark_queue_executed(int(queue_id))
                modified = True
                continue

            if queue_id:
                STORAGE.record_queue_attempt(int(queue_id))

            insider_date = trade.get('insider_date')
            if isinstance(insider_date, str):
                try:
                    trade['insider_date'] = datetime.date.fromisoformat(insider_date)
                except ValueError:
                    pass

            trade.setdefault('direction', 'buy')
            success = place_simple_market_order(api, trade, capital_per_trade_usd)
            if success:
                open_positions.add(symbol)
                if queue_id:
                    STORAGE.mark_queue_executed(int(queue_id))
                heartbeat("order", note=f"buy:{symbol}")
                modified = True
                time.sleep(2)

    refresh_runtime_views()
    return modified


def process_insider_trades(
    api: REST,
    is_market_open: bool,
    pending_orders: dict,
    capital_per_trade_usd: float | None,
) -> bool:
    pending = ensure_pending_structure(pending_orders)
    pending_buy_ids = {order.get('trade_id') for order in pending['buy']}

    try:
        open_positions = {p.symbol for p in api.list_positions()}
    except Exception as e:
        log.error(f"Could not load open positions: {e}")
        open_positions = set()

    can_trade_now = is_market_open and capital_per_trade_usd is not None

    seen_trade_ids = load_seen_trade_ids()
    latest_trades_df = fetch_insider_trades()
    RUNTIME_STATE.set_latest_scrape_rows(len(latest_trades_df.index))
    heartbeat("scrape", note=f"rows={len(latest_trades_df)}")
    if latest_trades_df.empty:
        log.info("No insider trades retrieved in this scan.")
        return False

    new_unseen_trades = latest_trades_df[~latest_trades_df['trade_id'].isin(seen_trade_ids)]
    if new_unseen_trades.empty:
        log.info("No new insider trades found since the last scan.")
        return False

    log.info(f"Found {len(new_unseen_trades)} new insider trades to evaluate.")

    queued_count = 0
    for _, trade in new_unseen_trades.iterrows():
        trade_info = trade.to_dict()
        trade_id = trade_info['trade_id']
        ticker = trade_info['ticker']
        STORAGE.upsert_signal(trade_info)

        if trade_info.get("filter_reason"):
            STORAGE.update_signal_status(trade_id, "filtered", trade_info["filter_reason"])
            STORAGE.mark_trade_seen(trade_id)
            log.info(f"Filtered {ticker} ({trade_id}) as {trade_info['filter_reason']}.")
            continue

        if trade_info.get("direction") is None:
            STORAGE.update_signal_status(trade_id, "filtered", "unsupported transaction type")
            STORAGE.mark_trade_seen(trade_id)
            continue

        if ticker in open_positions:
            log.info(f"Skipping {ticker}: position already open.")
            STORAGE.update_signal_status(trade_id, "skipped", "position already open")
            STORAGE.mark_trade_seen(trade_id)
            continue
        if trade_id in pending_buy_ids:
            log.info(f"Skipping {ticker}: trade {trade_id} already queued.")
            continue

        if can_trade_now:
            if capital_per_trade_usd is None:
                log.error("Capital per trade missing despite market open; cannot trade now.")
                STORAGE.update_signal_status(trade_id, "observed", "capital unavailable")
                heartbeat("order", ok=False, note=f"buy:{ticker}-no_capital")
                continue
            success = place_simple_market_order(api, trade_info, capital_per_trade_usd)
            if success:
                open_positions.add(ticker)
                heartbeat("order", note=f"buy:{ticker}")
                time.sleep(2)
            else:
                STORAGE.update_signal_status(trade_id, "observed", "submission failed; will retry")
                log.error(f"Failed to submit order for {ticker} during market hours.")
        else:
            if queue_pending_trade(pending_orders, trade_info):
                pending_buy_ids.add(trade_id)
                queued_count += 1

    if queued_count:
        log.info(f"Queued {queued_count} trades for the next market session.")

    refresh_runtime_views()
    return queued_count > 0

def place_simple_market_order(api: REST, trade_details, capital_usd: float) -> bool:
    """
    Places a market order.
    - Tries fractional for buys, falls back to whole shares if asset is not fractionable.
    - Uses whole shares for sells.
    """
    if isinstance(trade_details, pd.Series):
        trade_info = trade_details.to_dict()
    else:
        trade_info = dict(trade_details)

    symbol = trade_info.get('ticker'); side = trade_info.get('direction')
    if not symbol or not side:
        log.error("Trade details missing symbol or side. Cannot submit order.")
        return False
    try:
        latest_price = api.get_latest_trade(symbol).price
    except Exception as e:
        log.error(f"Could not get latest price for {symbol}. Skipping. Error: {e}"); return False

    if side == 'buy':
        qty = round(capital_usd / latest_price, 4)
        if qty <= 0.0001:
            log.warning(f"Capital of ${capital_usd:.2f} is too low to trade {symbol}."); return False
    elif side == 'sell':
        qty = int(capital_usd / latest_price)
        if qty < 1:
            log.warning(f"Capital of ${capital_usd:.2f} is too low to short 1 share of {symbol}."); return False
    else:
        log.error(f"Unknown side '{side}' for {symbol}."); return False

    if side == 'buy':
        tp_price = round(latest_price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
    else:
        tp_price = round(latest_price * (1 - TAKE_PROFIT_PERCENT / 100), 2)
    sl_price = None
    
    log.info(
        "Submitting %s MARKET order for %s shares of %s from %s [%s | %s].",
        side,
        qty,
        symbol,
        trade_info.get('insider') or 'unknown insider',
        trade_info.get('relationship') or 'unknown relationship',
        trade_info.get('transaction_type') or 'unknown transaction',
    )
    try:
        # --- PLAN A: Submit the order as calculated ---
        order = api.submit_order(symbol=symbol, qty=qty, side=side, type='market', time_in_force='day')
        log_trade_to_history(trade_info, order, latest_price, tp_price, sl_price)
        return True
    except APIError as e:
        # --- PLAN B: If it's a "not fractionable" error on a buy, retry with whole shares ---
        if "not fractionable" in str(e).lower() and side == 'buy':
            log.warning(f"Asset {symbol} is not fractionable. Retrying with whole shares.")
            whole_qty = int(qty)
            if whole_qty < 1:
                log.error(f"Cannot retry {symbol}: not enough capital for 1 whole share.")
                return False
            try:
                # Retry the order with the new integer quantity
                log.info(f"Submitting {side} MARKET order for {whole_qty} (whole) shares of {symbol}.")
                order = api.submit_order(symbol=symbol, qty=whole_qty, side=side, type='market', time_in_force='day')
                log_trade_to_history(trade_info, order, latest_price, tp_price, sl_price)
                return True
            except Exception as retry_e:
                log.error(f"Retry attempt for {symbol} also failed: {retry_e}")
                return False
        else:
            # For any other error, just log it and fail.
            log.error(f"Alpaca API error placing order for {symbol}: {e}")
            return False
    except Exception as e:
        log.error(f"An unknown error occurred placing order for {symbol}: {e}")
        return False

def check_and_manage_positions(api: REST, trade_history_df: pd.DataFrame, is_market_open: bool, pending_orders: dict) -> bool:
    log.info("Checking open positions for exit signals...")
    try:
        positions = api.list_positions()
    except Exception as e:
        log.error(f"Could not list open positions: {e}")
        return False

    if not positions:
        log.info("No open positions to manage.")
        return False

    pending_modified = False

    for pos in positions:
        try:
            current_price = api.get_latest_trade(pos.symbol).price
            trade_record = trade_history_df[trade_history_df['ticker'] == pos.symbol].tail(1)
            if trade_record.empty or 'take_profit_price' not in trade_record.columns:
                continue

            try:
                tp_price = float(trade_record['take_profit_price'].iloc[0])
            except (TypeError, ValueError):
                log.warning(f"Invalid take profit price for {pos.symbol}; skipping exit check.")
                continue

            exit_reason = None
            if pos.side == 'long' and current_price >= tp_price:
                exit_reason = f"take profit at ${tp_price}"
            elif pos.side == 'short' and current_price <= tp_price:
                exit_reason = f"take profit at ${tp_price}"

            if exit_reason:
                if is_market_open:
                    try:
                        api.close_position(pos.symbol)
                        log.info(f"Closed {pos.symbol} due to {exit_reason}.")
                        update_trade_exit_in_history(pos.symbol, exit_reason, current_price)
                        heartbeat("order", note=f"exit:{pos.symbol}")
                        pending_modified = True
                    except Exception as e:
                        log.error(f"Failed to close {pos.symbol}: {e}")
                        if queue_pending_sell(pending_orders, pos.symbol, exit_reason):
                            heartbeat("order", note=f"queue_exit:{pos.symbol}")
                            pending_modified = True
                else:
                    if queue_pending_sell(pending_orders, pos.symbol, exit_reason):
                        heartbeat("order", note=f"queue_exit:{pos.symbol}")
                        pending_modified = True
        except Exception as e:
            log.error(f"Error managing position for {pos.symbol}: {e}")

    return pending_modified

# --------------------------------------------------
# 3. Main Execution Loop
# --------------------------------------------------
if __name__ == '__main__':
    STORAGE.initialize()
    refresh_trade_history_metrics()
    refresh_runtime_views()
    if CONFIG.monitoring_enabled:
        try:
            start_monitoring_server(
                RUNTIME_STATE,
                host=CONFIG.monitoring_host,
                port=CONFIG.monitoring_port,
                log=log,
            )
            log.info(
                "Monitoring server listening on http://%s:%s",
                CONFIG.monitoring_host,
                CONFIG.monitoring_port,
            )
        except Exception as exc:
            log.error(f"Failed to start monitoring server: {exc}")

    api = REST(key_id=API_KEY, secret_key=SECRET_KEY, base_url=BASE_URL)
    try:
        orig_request = api._session.request
        api._session.request = functools.partial(orig_request, timeout=REQUEST_TIMEOUT)
        log.info("Applied HTTP timeout to Alpaca session.")
    except Exception:
        log.warning("Could not attach timeout to Alpaca session; continuing anyway.")

    log.info("--- Starting Continuous Insider Trading Bot with Smart Error Handling ---")
    log.info("State directory: %s", CONFIG.state_dir)
    log.info("SQLite state DB: %s", STATE_DB_PATH)
    log.info("Trade history file: %s", TRADE_HISTORY_CSV)
    log.info("Pending orders file: %s", PENDING_ORDERS_JSON)

    last_insider_scan = datetime.datetime.min
    last_position_check = datetime.datetime.min

    while True:
        heartbeat("loop_start")
        cycle_start = datetime.datetime.utcnow()
        pending_orders = load_pending_orders()
        pending_modified = False
        is_market_open = False
        next_open_at: datetime.datetime | None = None

        try:
            try:
                clock = api.get_clock()
                is_market_open = clock.is_open
                next_open_at = normalize_utc_datetime(getattr(clock, "next_open", None))
                RUNTIME_STATE.set_market_open(is_market_open)
                log.info(f"Market open status: {is_market_open}")
                heartbeat("alpaca_clock", note="open" if is_market_open else "closed")
            except Exception as e:
                log.error(f"Could not retrieve market clock: {e}")
                RUNTIME_STATE.set_market_open(None)
                heartbeat("alpaca_clock", ok=False, note=str(e))
                is_market_open = False

            capital_per_trade_usd: float | None = None
            if is_market_open:
                log.info("Step: fetch FX rate")
                usd_per_czk = get_usd_per_czk()
                if usd_per_czk is None:
                    log.error("Cannot compute USD capital this cycle; deferring USD-denominated actions.")
                    heartbeat("fx", ok=False, note="rate_unavailable")
                else:
                    capital_per_trade_usd = TRADE_CAPITAL_CZK * usd_per_czk
                    log.info(f"{TRADE_CAPITAL_CZK} CZK ~= ${capital_per_trade_usd:.2f} USD")
                    heartbeat("fx", note=f"{usd_per_czk:.4f}")
            else:
                heartbeat("fx", note="market_closed")

            if is_market_open:
                try:
                    executed_pending = execute_pending_orders(api, pending_orders, capital_per_trade_usd)
                    if executed_pending:
                        pending_modified = True
                        heartbeat("pending_orders", note="executed")
                    else:
                        if capital_per_trade_usd is None and pending_orders.get('buy'):
                            heartbeat("pending_orders", ok=False, note="no_capital")
                        else:
                            heartbeat("pending_orders", note="no_changes")
                except Exception as e:
                    log.error(f"Error while processing queued orders: {e}")
                    heartbeat("pending_orders", ok=False, note=str(e))
            else:
                if pending_orders.get('buy') or pending_orders.get('sell'):
                    heartbeat("pending_orders", note="market_closed_with_queue")
                else:
                    heartbeat("pending_orders", note="market_closed")

            ran_insider_scan = False
            insider_scan_interval_minutes = effective_insider_scan_interval_minutes(is_market_open)
            if (cycle_start - last_insider_scan) >= datetime.timedelta(minutes=insider_scan_interval_minutes):
                ran_insider_scan = True
                try:
                    if process_insider_trades(api, is_market_open, pending_orders, capital_per_trade_usd):
                        pending_modified = True
                except Exception as e:
                    log.error(f"Error while processing insider trades: {e}")
                    heartbeat("scrape", ok=False, note=str(e))
                finally:
                    last_insider_scan = cycle_start
            if not ran_insider_scan:
                heartbeat("scrape", note="skipped_interval")

            run_position_check = is_market_open and (
                (cycle_start - last_position_check)
                >= datetime.timedelta(minutes=effective_position_check_interval_minutes())
            )
            if run_position_check:
                try:
                    trade_history_df = load_trade_history()
                    if not trade_history_df.empty and check_and_manage_positions(api, trade_history_df, True, pending_orders):
                        pending_modified = True
                        heartbeat("manage_positions", note="executed")
                    elif trade_history_df.empty:
                        heartbeat("manage_positions", note="no_history")
                    else:
                        heartbeat("manage_positions", note="no_changes")
                except Exception as e:
                    log.error(f"Error during position management: {e}")
                    heartbeat("manage_positions", ok=False, note=str(e))
                finally:
                    last_position_check = cycle_start
            else:
                heartbeat(
                    "manage_positions",
                    note="market_closed" if not is_market_open else "skipped_interval",
                )

        except Exception as loop_exception:
            log.critical(f"Critical error in main loop: {loop_exception}", exc_info=True)
            heartbeat("loop_exception", ok=False, note=str(loop_exception))

        if pending_modified:
            save_pending_orders(pending_orders)
        else:
            refresh_runtime_views()

        pending_orders = load_pending_orders()
        sleep_seconds = compute_sleep_seconds(
            is_market_open=is_market_open,
            pending_orders=pending_orders,
            next_open_at=next_open_at,
        )
        market_state = "open" if is_market_open else "closed"
        log.info(
            f"Cycle complete. Sleeping for {sleep_seconds} seconds (market {market_state})."
        )
        heartbeat("sleep", note=f"{sleep_seconds}s_{market_state}")
        time.sleep(sleep_seconds)
