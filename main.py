import datetime
import functools
import json
import logging
import math
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

# --- Basic Setup ---
REQUEST_TIMEOUT = (10, 25)  # (connect, read) seconds
HEARTBEAT_FILE = 'heartbeat.txt'
APP_LOG_FILE = 'app.log'

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
        with open(HEARTBEAT_FILE, "a") as hb:
            hb.write(line)
            hb.flush()
            os.fsync(hb.fileno())
    except Exception as exc:
        log.error(f"Failed to write heartbeat: {exc}")

# --- ALPACA API CONFIGURATION ---
DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"

# --- State Management & Parameters ---
TRADE_HISTORY_CSV = 'trade_history.csv'
SEEN_TRADES_LOG = 'seen_insider_trades.log'
SEEN_GROUPS_LOG = 'seen_insider_groups.log'
PENDING_ORDERS_JSON = 'pending_orders.json'

TRADE_CAPITAL_CZK = 250.0
TAKE_PROFIT_PERCENT = 10.0
INSIDER_SCAN_INTERVAL_MINUTES = 15
POSITION_CHECK_INTERVAL_MINUTES = 5
MARKET_OPEN_POLL_SECONDS = 60
MARKET_CLOSED_POLL_SECONDS = INSIDER_SCAN_INTERVAL_MINUTES * 60


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

def parse_finviz_date(raw: str) -> datetime.date | None:
    # This function is from your script and remains unchanged.
    try: return datetime.datetime.strptime(raw.strip(), "%b %d '%y").date()
    except ValueError:
        try:
            dt = datetime.datetime.strptime(raw.strip(), "%b %d").date()
            return dt.replace(year=datetime.date.today().year)
        except Exception: return None

def infer_direction(txn: str) -> str | None:
    # This function is from your script and remains unchanged.
    lower = txn.lower()
    if 'buy' in lower: return 'buy'
    if 'sell' in lower or 'sale' in lower: return 'sell'
    return None


def load_alpaca_credentials() -> tuple[str, str, str]:
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    base_url = os.environ.get("APCA_API_BASE_URL", DEFAULT_BASE_URL)

    if not key or not secret:
        raise RuntimeError(
            "Missing Alpaca credentials. Set APCA_API_KEY_ID/APCA_API_SECRET_KEY or ALPACA_API_KEY/ALPACA_SECRET_KEY."
        )

    return key, secret, base_url

# --------------------------------------------------
# 1. Scrape insider transactions from Finviz
# --------------------------------------------------
def build_group_key(ticker: str, direction: str, owner: str | None, insider_date: datetime.date) -> str:
    owner_normalized = (owner or "").strip().lower()
    ticker_normalized = (ticker or "").strip().upper()
    direction_normalized = (direction or "").strip().lower()
    date_str = insider_date.isoformat() if isinstance(insider_date, datetime.date) else str(insider_date)
    return "|".join([ticker_normalized, direction_normalized, owner_normalized, date_str])


def fetch_insider_trades() -> pd.DataFrame:
    base_url = 'https://finviz.com/insidertrading.ashx'
    all_records = []
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
                direction = infer_direction(transaction)
                if direction is None:
                    continue
                cost = float(cost_raw.replace(',', ''))
                shares = int(shares_raw.replace(',', ''))
                date = parse_finviz_date(date_raw)
                if cost == 0 or not date:
                    continue
                trade_id = f"{date.isoformat()}-{ticker}-{direction}-{shares}"
                group_key = build_group_key(ticker, direction, owner, date)
                all_records.append(
                    {
                        'trade_id': trade_id,
                        'ticker': ticker,
                        'direction': direction,
                        'cost': cost,
                        'insider_date': date,
                        'insider': owner,
                        'group_key': group_key,
                    }
                )
            except (ValueError, KeyError, IndexError) as e:
                log.warning(f"Skipping a row due to parsing error: {e}")
                continue

        next_link = soup.find('a', string='next')
        if next_link and next_link.get('href'):
            current_url = 'https://finviz.com/' + next_link.get('href')
            time.sleep(1)
        else:
            log.info("No 'next' page link found. Scrape complete.")
            break

    df = pd.DataFrame(all_records)
    if not df.empty and 'group_key' in df.columns:
        df = df.drop_duplicates(subset=['group_key'], keep='first')
    return df

# --------------------------------------------------
# 2. Live Trading Logic and Persistent Memory
# --------------------------------------------------

def load_seen_trade_ids() -> set:
    if not os.path.exists(SEEN_TRADES_LOG):
        return set()
    with open(SEEN_TRADES_LOG, 'r') as f:
        return {line.strip() for line in f if line.strip()}


def load_seen_trade_groups() -> set:
    groups: set[str] = set()
    if os.path.exists(SEEN_GROUPS_LOG):
        with open(SEEN_GROUPS_LOG, 'r') as f:
            groups.update(line.strip() for line in f if line.strip())

    if os.path.exists(TRADE_HISTORY_CSV):
        try:
            df = pd.read_csv(TRADE_HISTORY_CSV)
            if not df.empty:
                if 'insider_group_key' in df.columns:
                    history_groups = {
                        str(value)
                        for value in df['insider_group_key']
                        if isinstance(value, str) and value.strip()
                    }
                    groups.update(history_groups)
                else:
                    fallback_groups = set()
                    if {'ticker', 'side', 'insider_date'}.issubset(df.columns):
                        for _, row in df.iterrows():
                            ticker = row.get('ticker')
                            direction = row.get('side')
                            insider_date = row.get('insider_date')
                            insider = row.get('insider_name') if 'insider_name' in df.columns else None
                            if pd.isna(ticker) or pd.isna(direction) or pd.isna(insider_date):
                                continue
                            try:
                                insider_dt = datetime.date.fromisoformat(str(insider_date)[:10])
                            except ValueError:
                                continue
                            fallback_groups.add(
                                build_group_key(str(ticker), str(direction), str(insider) if insider is not None else None, insider_dt)
                            )
                    groups.update(fallback_groups)
        except Exception:
            pass

    return groups

def log_trades_as_seen(new_trade_ids: list):
    with open(SEEN_TRADES_LOG, 'a') as f:
        for trade_id in new_trade_ids:
            f.write(f"{trade_id}\n")
    log.info(f"Logged {len(new_trade_ids)} new trades to {SEEN_TRADES_LOG}")


def log_trade_groups_as_seen(group_keys: list[str]):
    if not group_keys:
        return
    with open(SEEN_GROUPS_LOG, 'a') as f:
        for group_key in group_keys:
            f.write(f"{group_key}\n")
    log.info(f"Logged {len(group_keys)} trade groups to {SEEN_GROUPS_LOG}")

def load_trade_history() -> pd.DataFrame:
    if not os.path.exists(TRADE_HISTORY_CSV): return pd.DataFrame(columns=['trade_id'])
    try: return pd.read_csv(TRADE_HISTORY_CSV)
    except pd.errors.EmptyDataError: return pd.DataFrame(columns=['trade_id'])

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
        'insider_group_key': trade_info.get('group_key'),
        'estimated_entry_price': entry_price, 'take_profit_price': tp_price, 'stop_loss_price': sl_price,
        'alpaca_order_id': order_obj.id, 'status': order_obj.status, 'exit_reason': None,
        'exit_timestamp_utc': None
    }
    file_exists = os.path.exists(TRADE_HISTORY_CSV)
    df = pd.DataFrame([record])
    df.to_csv(TRADE_HISTORY_CSV, mode='a', header=not file_exists, index=False)
    log.info(f"Successfully logged trade execution {record['trade_id']} to {TRADE_HISTORY_CSV}")


def update_trade_exit_in_history(symbol: str, exit_reason: str):
    if not os.path.exists(TRADE_HISTORY_CSV):
        return
    try:
        df = pd.read_csv(TRADE_HISTORY_CSV)
    except pd.errors.EmptyDataError:
        return

    if df.empty or 'ticker' not in df.columns:
        return

    if 'exit_reason' not in df.columns:
        df['exit_reason'] = None
    if 'exit_timestamp_utc' not in df.columns:
        df['exit_timestamp_utc'] = None

    mask = df['ticker'] == symbol
    if not mask.any():
        return

    idx = df[mask].index[-1]
    df.loc[idx, 'exit_reason'] = exit_reason
    df.loc[idx, 'exit_timestamp_utc'] = datetime.datetime.utcnow().isoformat()
    df.to_csv(TRADE_HISTORY_CSV, index=False)
    log.info(f"Updated trade history for {symbol} with exit reason '{exit_reason}'.")


def ensure_pending_structure(data: dict | None) -> dict:
    if not isinstance(data, dict):
        data = {}
    data.setdefault('buy', [])
    data.setdefault('sell', [])
    return data


def load_pending_orders() -> dict:
    if not os.path.exists(PENDING_ORDERS_JSON):
        return {'buy': [], 'sell': []}
    try:
        with open(PENDING_ORDERS_JSON, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Could not read {PENDING_ORDERS_JSON}: {e}. Starting with empty queue.")
        return {'buy': [], 'sell': []}
    return ensure_pending_structure(data)


def save_pending_orders(pending_orders: dict):
    data = ensure_pending_structure(pending_orders)
    with open(PENDING_ORDERS_JSON, 'w') as f:
        json.dump(data, f, indent=2)
    log.info(f"Persisted pending orders queue to {PENDING_ORDERS_JSON}.")


def queue_pending_trade(pending_orders: dict, trade_details) -> bool:
    pending = ensure_pending_structure(pending_orders)
    trade_info = trade_details.to_dict() if isinstance(trade_details, pd.Series) else dict(trade_details)
    trade_id = trade_info.get('trade_id')
    ticker = trade_info.get('ticker')
    group_key = trade_info.get('group_key')
    if not trade_id or not ticker:
        log.warning("Cannot queue trade without trade_id and ticker.")
        return False

    existing_ids = {order.get('trade_id') for order in pending['buy']}
    if trade_id in existing_ids:
        log.info(f"Trade {trade_id} for {ticker} is already queued for execution.")
        return False

    if group_key:
        existing_groups = {order.get('group_key') for order in pending['buy'] if order.get('group_key')}
        if group_key in existing_groups:
            log.info(f"Trade group {group_key} for {ticker} already queued; skipping duplicate.")
            return False

    insider_date = trade_info.get('insider_date')
    if isinstance(insider_date, (datetime.date, datetime.datetime)):
        insider_date_str = insider_date.isoformat()
    elif insider_date is not None:
        insider_date_str = str(insider_date)
    else:
        insider_date_str = None

    pending['buy'].append({
        'trade_id': trade_id,
        'ticker': ticker,
        'direction': trade_info.get('direction', 'buy'),
        'insider_date': insider_date_str,
        'insider': trade_info.get('insider'),
        'group_key': group_key,
        'queued_at_utc': datetime.datetime.utcnow().isoformat()
    })
    log.info(f"Queued buy order for {ticker} ({trade_id}) until market hours.")
    return True


def queue_pending_sell(pending_orders: dict, symbol: str, reason: str) -> bool:
    if not symbol:
        log.warning("Cannot queue sell order without a symbol.")
        return False

    pending = ensure_pending_structure(pending_orders)
    existing = {(order.get('symbol'), order.get('reason')) for order in pending['sell']}
    key = (symbol, reason)
    if key in existing:
        log.info(f"Sell order for {symbol} ({reason}) is already queued.")
        return False

    pending['sell'].append({
        'symbol': symbol,
        'reason': reason,
        'queued_at_utc': datetime.datetime.utcnow().isoformat()
    })
    log.info(f"Queued sell order for {symbol} due to {reason}.")
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
        remaining_sell_orders = []
        for order in sell_orders:
            symbol = order.get('symbol')
            reason = order.get('reason', 'pending sell')
            if not symbol:
                continue
            try:
                api.close_position(symbol)
                log.info(f"Executed queued sell for {symbol} ({reason}).")
                update_trade_exit_in_history(symbol, reason)
                heartbeat("order", note=f"sell:{symbol}")
                modified = True
            except Exception as e:
                log.error(f"Failed to execute queued sell for {symbol}: {e}")
                remaining_sell_orders.append(order)
        pending['sell'] = remaining_sell_orders
        if len(remaining_sell_orders) != len(sell_orders):
            modified = True

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

        remaining_buy_orders = []
        for order in buy_orders:
            trade = dict(order)
            symbol = trade.get('ticker')
            if not symbol:
                log.warning("Skipping queued buy order without a ticker symbol.")
                continue
            if symbol in open_positions:
                log.info(f"Skipping queued buy for {symbol}: position already open.")
                modified = True
                continue

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
                heartbeat("order", note=f"buy:{symbol}")
                modified = True
                time.sleep(2)
            else:
                remaining_buy_orders.append(order)

        pending['buy'] = remaining_buy_orders
        if len(remaining_buy_orders) != len(buy_orders):
            modified = True

    return modified


def process_insider_trades(
    api: REST,
    is_market_open: bool,
    pending_orders: dict,
    capital_per_trade_usd: float | None,
) -> bool:
    pending = ensure_pending_structure(pending_orders)
    pending_buy_ids = {order.get('trade_id') for order in pending['buy']}
    pending_group_keys = {order.get('group_key') for order in pending['buy'] if order.get('group_key')}

    try:
        open_positions = {p.symbol for p in api.list_positions()}
    except Exception as e:
        log.error(f"Could not load open positions: {e}")
        open_positions = set()

    can_trade_now = is_market_open and capital_per_trade_usd is not None

    seen_trade_ids = load_seen_trade_ids()
    seen_trade_groups = load_seen_trade_groups()
    latest_trades_df = fetch_insider_trades()
    heartbeat("scrape", note=f"rows={len(latest_trades_df)}")
    if latest_trades_df.empty:
        log.info("No insider trades retrieved in this scan.")
        return False

    if 'group_key' not in latest_trades_df.columns:
        latest_trades_df['group_key'] = latest_trades_df.apply(
            lambda row: build_group_key(
                row.get('ticker'),
                row.get('direction'),
                row.get('insider'),
                row.get('insider_date'),
            ),
            axis=1,
        )

    new_unseen_trades = latest_trades_df[
        (~latest_trades_df['trade_id'].isin(seen_trade_ids))
        & (~latest_trades_df['group_key'].isin(seen_trade_groups))
    ]
    if new_unseen_trades.empty:
        log.info("No new insider trades found since the last scan.")
        return False

    log_trades_as_seen(new_unseen_trades['trade_id'].tolist())
    log_trade_groups_as_seen([
        key for key in new_unseen_trades['group_key'].tolist() if isinstance(key, str) and key
    ])
    log.info(f"Found {len(new_unseen_trades)} new insider trades to evaluate.")

    queued_count = 0
    for _, trade in new_unseen_trades.iterrows():
        ticker = trade['ticker']
        trade_id = trade['trade_id']
        group_key = trade.get('group_key')
        if ticker in open_positions:
            log.info(f"Skipping {ticker}: position already open.")
            continue
        if trade_id in pending_buy_ids:
            log.info(f"Skipping {ticker}: trade {trade_id} already queued.")
            continue
        if group_key and group_key in pending_group_keys:
            log.info(f"Skipping {ticker}: trade group {group_key} already queued.")
            continue

        if can_trade_now:
            if capital_per_trade_usd is None:
                log.error("Capital per trade missing despite market open; cannot trade now.")
                heartbeat("order", ok=False, note=f"buy:{ticker}-no_capital")
                continue
            success = place_simple_market_order(api, trade, capital_per_trade_usd)
            if success:
                open_positions.add(ticker)
                if group_key:
                    pending_group_keys.add(group_key)
                heartbeat("order", note=f"buy:{ticker}")
                time.sleep(2)
            else:
                log.error(f"Failed to submit order for {ticker} during market hours.")
        else:
            if queue_pending_trade(pending_orders, trade):
                pending_buy_ids.add(trade_id)
                if group_key:
                    pending_group_keys.add(group_key)
                queued_count += 1

    if queued_count:
        log.info(f"Queued {queued_count} trades for the next market session.")

    return queued_count > 0

def _calculate_order_quantity(latest_price: float, capital_usd: float, fractionable: bool) -> float | int | None:
    if latest_price <= 0:
        return None

    target_shares = capital_usd / latest_price
    if fractionable:
        precise_shares = math.floor(target_shares * 10_000) / 10_000
        if precise_shares <= 0:
            return None
        return precise_shares

    floor_shares = max(1, math.floor(target_shares))
    ceil_shares = max(floor_shares, math.ceil(target_shares))

    floor_value = floor_shares * latest_price
    ceil_value = ceil_shares * latest_price
    target_value = capital_usd

    floor_diff = abs(target_value - floor_value)
    ceil_diff = abs(target_value - ceil_value)

    if ceil_diff < floor_diff and ceil_value <= target_value * 1.1:
        return ceil_shares
    return floor_shares


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
        log.error(f"Could not get latest price for {symbol}. Skipping. Error: {e}")
        return False

    if side not in {'buy', 'sell'}:
        log.error(f"Unknown side '{side}' for {symbol}.")
        return False

    fractionable = True
    if side == 'buy':
        try:
            asset = api.get_asset(symbol)
            fractionable = bool(getattr(asset, 'fractionable', True))
        except Exception as e:
            log.warning(f"Could not determine if {symbol} is fractionable: {e}. Assuming fractionable.")

    qty = _calculate_order_quantity(latest_price, capital_usd, fractionable if side == 'buy' else False)
    if qty is None:
        log.warning(f"Capital of ${capital_usd:.2f} is too low to trade {symbol} at ${latest_price:.2f}.")
        return False

    if side == 'buy':
        tp_price = round(latest_price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
    else:
        tp_price = round(latest_price * (1 - TAKE_PROFIT_PERCENT / 100), 2)
    sl_price = None
    
    log.info(f"Submitting {side} MARKET order for {qty} shares of {symbol}.")
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
                        update_trade_exit_in_history(pos.symbol, exit_reason)
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
    try:
        api_key, api_secret, api_base_url = load_alpaca_credentials()
    except RuntimeError as credential_error:
        log.critical(f"Cannot start bot without Alpaca credentials: {credential_error}")
        sys.exit(1)

    api = REST(key_id=api_key, secret_key=api_secret, base_url=api_base_url)
    try:
        orig_request = api._session.request
        api._session.request = functools.partial(orig_request, timeout=REQUEST_TIMEOUT)
        log.info("Applied HTTP timeout to Alpaca session.")
    except Exception:
        log.warning("Could not attach timeout to Alpaca session; continuing anyway.")

    log.info("--- Starting Continuous Insider Trading Bot with Smart Error Handling ---")

    last_insider_scan = datetime.datetime.min
    last_position_check = datetime.datetime.min

    while True:
        heartbeat("loop_start")
        cycle_start = datetime.datetime.utcnow()
        pending_orders = load_pending_orders()
        pending_modified = False
        is_market_open = False

        try:
            try:
                clock = api.get_clock()
                is_market_open = clock.is_open
                log.info(f"Market open status: {is_market_open}")
                heartbeat("alpaca_clock", note="open" if is_market_open else "closed")
            except Exception as e:
                log.error(f"Could not retrieve market clock: {e}")
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
                    log.info(f"{TRADE_CAPITAL_CZK} CZK ≈ ${capital_per_trade_usd:.2f} USD")
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
            if (cycle_start - last_insider_scan) >= datetime.timedelta(minutes=INSIDER_SCAN_INTERVAL_MINUTES):
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
                (cycle_start - last_position_check) >= datetime.timedelta(minutes=POSITION_CHECK_INTERVAL_MINUTES)
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

        sleep_seconds = (
            MARKET_OPEN_POLL_SECONDS if is_market_open else MARKET_CLOSED_POLL_SECONDS
        )
        market_state = "open" if is_market_open else "closed"
        log.info(
            f"Cycle complete. Sleeping for {sleep_seconds} seconds (market {market_state})."
        )
        heartbeat("sleep", note=f"{sleep_seconds}s_{market_state}")
        time.sleep(sleep_seconds)
