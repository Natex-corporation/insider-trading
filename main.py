import datetime
import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup
import os
import time
import json
import re
from alpaca_trade_api.rest import REST, APIError

# --- Basic Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

# --- ALPACA API CONFIGURATION (Using hardcoded keys as requested for testing) ---
# !!! WARNING: These keys are exposed and should be revoked after testing.
API_KEY = "PKDBS69E76P0DIEJF5AR"
SECRET_KEY = "9xnzc0fSSfduish5ocaHIYkOpaBapZChTSX2AqRf"
BASE_URL = "https://paper-api.alpaca.markets"

# --- State Management & Parameters ---
TRADE_HISTORY_CSV = 'trade_history.csv'
SEEN_TRADES_LOG = 'seen_insider_trades.log'
TRADE_CAPITAL_CZK = 250.0
POSITION_HOLD_LIMIT_DAYS = 30
TAKE_PROFIT_PERCENT = 10.0
STOP_LOSS_PERCENT = 10.0
CHECK_INTERVAL_MINUTES = 15


def get_usd_per_czk() -> float | None:
    # This function is from your script and remains unchanged.
    try:
        url = "https://api.exchangerate-api.com/v4/latest/CZK"
        response = requests.get(url, timeout=10); response.raise_for_status()
        data = response.json(); rate = data['rates']['USD']
        logging.info(f"Fetched CZK to USD exchange rate: {rate}"); return float(rate)
    except Exception as e:
        logging.error(f"Could not fetch CZK to USD exchange rate: {e}"); return None

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

# --------------------------------------------------
# 1. Scrape insider transactions from Finviz
# --------------------------------------------------
def fetch_insider_trades() -> pd.DataFrame:
    # This function is from your script and remains unchanged.
    base_url = 'https://finviz.com/insidertrading.ashx'; headers = {'User-Agent': 'Mozilla/5.0'}
    all_records = []; current_url = base_url
    while True:
        logging.info(f"Fetching insider trades from: {current_url}")
        try:
            resp = requests.get(current_url, headers=headers); resp.raise_for_status()
        except requests.RequestException as e: logging.error(f"Failed to fetch Finviz page: {e}"); break
        soup = BeautifulSoup(resp.text, 'html.parser'); target_table = None; header_map = {}
        for table in soup.find_all('table'):
            first_row = table.find('tr')
            if not first_row: continue
            header_cells = [cell.get_text(strip=True) for cell in first_row.find_all(['td', 'th'])]
            normalized_headers = [re.sub(r'[^a-zA-Z0-9]', '', h).lower() for h in header_cells]
            if 'ticker' in normalized_headers and 'transaction' in normalized_headers and 'shares' in normalized_headers:
                header_map = {name: i for i, name in enumerate(normalized_headers)}; target_table = table; break
        if not target_table: logging.error("Could not find a valid insider trading table on the page."); break
        rows = target_table.find_all('tr')[1:]
        if not rows: logging.info("No more trade rows found on this page. Ending scrape."); break
        for tr in rows:
            tds = tr.find_all('td')
            if len(tds) < len(header_map): continue
            try:
                ticker = tds[header_map['ticker']].text.strip(); transaction = tds[header_map['transaction']].text.strip()
                date_raw = tds[header_map['date']].text.strip(); cost_raw = tds[header_map['cost']].text.strip()
                shares_raw = tds[header_map['shares']].text.strip()
                owner = tds[header_map.get('owner', header_map.get('insidername', -1))].text.strip()
                direction = infer_direction(transaction)
                if direction is None: continue
                cost = float(cost_raw.replace(',', '')); shares = int(shares_raw.replace(',', ''))
                date = parse_finviz_date(date_raw)
                if cost == 0 or not date: continue
                trade_id = f"{date.isoformat()}-{ticker}-{direction}-{shares}"
                all_records.append({'trade_id': trade_id, 'ticker': ticker, 'direction': direction, 'cost': cost, 'insider_date': date, 'insider': owner})
            except (ValueError, KeyError, IndexError) as e: logging.warning(f"Skipping a row due to parsing error: {e}"); continue
        next_link = soup.find('a', string='next')
        if next_link and next_link.get('href'):
            current_url = 'https://finviz.com/' + next_link.get('href'); time.sleep(1)
        else:
            logging.info("No 'next' page link found. Scrape complete."); break
    return pd.DataFrame(all_records)

# --------------------------------------------------
# 2. Live Trading Logic and Persistent Memory
# --------------------------------------------------

def load_seen_trade_ids() -> set:
    if not os.path.exists(SEEN_TRADES_LOG): return set()
    with open(SEEN_TRADES_LOG, 'r') as f: return {line.strip() for line in f}

def log_trades_as_seen(new_trade_ids: list):
    with open(SEEN_TRADES_LOG, 'a') as f:
        for trade_id in new_trade_ids: f.write(f"{trade_id}\n")
    logging.info(f"Logged {len(new_trade_ids)} new trades to {SEEN_TRADES_LOG}")

def load_trade_history() -> pd.DataFrame:
    if not os.path.exists(TRADE_HISTORY_CSV): return pd.DataFrame(columns=['trade_id'])
    try: return pd.read_csv(TRADE_HISTORY_CSV)
    except pd.errors.EmptyDataError: return pd.DataFrame(columns=['trade_id'])

def log_trade_to_history(trade_details: pd.Series, order_obj, entry_price: float, tp_price: float, sl_price: float):
    record = {
        'trade_id': trade_details['trade_id'], 'timestamp_utc': datetime.datetime.utcnow().isoformat(),
        'ticker': order_obj.symbol, 'side': order_obj.side, 'order_qty': order_obj.qty,
        'insider_date': trade_details['insider_date'], 'insider_name': trade_details['insider'],
        'estimated_entry_price': entry_price, 'take_profit_price': tp_price, 'stop_loss_price': sl_price,
        'alpaca_order_id': order_obj.id, 'status': order_obj.status, 'exit_reason': None
    }
    file_exists = os.path.exists(TRADE_HISTORY_CSV)
    df = pd.DataFrame([record])
    df.to_csv(TRADE_HISTORY_CSV, mode='a', header=not file_exists, index=False)
    logging.info(f"Successfully logged trade execution {record['trade_id']} to {TRADE_HISTORY_CSV}")

def place_simple_market_order(api: REST, trade_details: pd.Series, capital_usd: float) -> bool:
    """
    Places a market order.
    - Tries fractional for buys, falls back to whole shares if asset is not fractionable.
    - Uses whole shares for sells.
    """
    symbol = trade_details['ticker']; side = trade_details['direction']
    try:
        latest_price = api.get_latest_trade(symbol).price
    except Exception as e:
        logging.error(f"Could not get latest price for {symbol}. Skipping. Error: {e}"); return False

    if side == 'buy':
        qty = round(capital_usd / latest_price, 4)
        if qty <= 0.0001:
            logging.warning(f"Capital of ${capital_usd:.2f} is too low to trade {symbol}."); return False
    elif side == 'sell':
        qty = int(capital_usd / latest_price)
        if qty < 1:
            logging.warning(f"Capital of ${capital_usd:.2f} is too low to short 1 share of {symbol}."); return False
    else:
        logging.error(f"Unknown side '{side}' for {symbol}."); return False

    if side == 'buy':
        tp_price = round(latest_price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
        sl_price = round(latest_price * (1 - STOP_LOSS_PERCENT / 100), 2)
    else:
        tp_price = round(latest_price * (1 - TAKE_PROFIT_PERCENT / 100), 2)
        sl_price = round(latest_price * (1 + STOP_LOSS_PERCENT / 100), 2)
    
    logging.info(f"Submitting {side} MARKET order for {qty} shares of {symbol}.")
    try:
        # --- PLAN A: Submit the order as calculated ---
        order = api.submit_order(symbol=symbol, qty=qty, side=side, type='market', time_in_force='day')
        log_trade_to_history(trade_details, order, latest_price, tp_price, sl_price)
        return True
    except APIError as e:
        # --- PLAN B: If it's a "not fractionable" error on a buy, retry with whole shares ---
        if "not fractionable" in str(e).lower() and side == 'buy':
            logging.warning(f"Asset {symbol} is not fractionable. Retrying with whole shares.")
            whole_qty = int(qty)
            if whole_qty < 1:
                logging.error(f"Cannot retry {symbol}: not enough capital for 1 whole share.")
                return False
            try:
                # Retry the order with the new integer quantity
                logging.info(f"Submitting {side} MARKET order for {whole_qty} (whole) shares of {symbol}.")
                order = api.submit_order(symbol=symbol, qty=whole_qty, side=side, type='market', time_in_force='day')
                log_trade_to_history(trade_details, order, latest_price, tp_price, sl_price)
                return True
            except Exception as retry_e:
                logging.error(f"Retry attempt for {symbol} also failed: {retry_e}")
                return False
        else:
            # For any other error, just log it and fail.
            logging.error(f"Alpaca API error placing order for {symbol}: {e}")
            return False
    except Exception as e:
        logging.error(f"An unknown error occurred placing order for {symbol}: {e}")
        return False

def check_and_manage_positions(api: REST, trade_history_df: pd.DataFrame):
    # This function is from your script and remains unchanged.
    logging.info("Checking open positions for exit signals...")
    try: positions = api.list_positions()
    except Exception as e: logging.error(f"Could not list open positions: {e}"); return
    if not positions: logging.info("No open positions to manage."); return
    for pos in positions:
        try:
            current_price = api.get_latest_trade(pos.symbol).price
            trade_record = trade_history_df[trade_history_df['ticker'] == pos.symbol].tail(1)
            if trade_record.empty: continue
            tp_price = trade_record['take_profit_price'].iloc[0]; sl_price = trade_record['stop_loss_price'].iloc[0]
            entry_time = datetime.datetime.fromisoformat(trade_record['timestamp_utc'].iloc[0])
            days_held = (datetime.datetime.utcnow() - entry_time).days
            exit_reason = None
            if pos.side == 'long':
                if current_price >= tp_price: exit_reason = f"take profit at ${tp_price}"
                elif current_price <= sl_price: exit_reason = f"stop loss at ${sl_price}"
            elif pos.side == 'short':
                if current_price <= tp_price: exit_reason = f"take profit at ${tp_price}"
                elif current_price >= sl_price: exit_reason = f"stop loss at ${sl_price}"
            if days_held > POSITION_HOLD_LIMIT_DAYS: exit_reason = f"time limit of {POSITION_HOLD_LIMIT_DAYS} days"
            if exit_reason:
                logging.info(f"Closing {pos.symbol} due to {exit_reason}.")
                api.close_position(pos.symbol)
        except Exception as e: logging.error(f"Error managing position for {pos.symbol}: {e}")

# --------------------------------------------------
# 3. Main Execution Loop
# --------------------------------------------------
if __name__ == '__main__':
    api = REST(key_id=API_KEY, secret_key=SECRET_KEY, base_url=BASE_URL)
    logging.info("--- Starting Continuous Insider Trading Bot with Smart Error Handling ---")
    
    while True:
        try:
            usd_per_czk = get_usd_per_czk()
            if usd_per_czk is None:
                logging.error("Halting check: could not get exchange rate."); time.sleep(60 * 5); continue
            capital_per_trade_usd = TRADE_CAPITAL_CZK * usd_per_czk
            logging.info(f"{TRADE_CAPITAL_CZK} CZK is approx ${capital_per_trade_usd:.2f} USD.")

            seen_trade_ids = load_seen_trade_ids()
            open_positions = {p.symbol for p in api.list_positions()}
            logging.info(f"Loaded {len(seen_trade_ids)} seen trade IDs. Holding {len(open_positions)} positions: {list(open_positions)}")

            latest_trades_df = fetch_insider_trades()
            if not latest_trades_df.empty:
                new_unseen_trades = latest_trades_df[~latest_trades_df['trade_id'].isin(seen_trade_ids)]
                if not new_unseen_trades.empty:
                    logging.info(f"Found {len(new_unseen_trades)} new, unseen insider trades.")
                    log_trades_as_seen(new_unseen_trades['trade_id'].tolist())
                    for _, trade in new_unseen_trades.iterrows():
                        if trade['ticker'] in open_positions:
                            logging.info(f"Skipping trade for {trade['ticker']}: position already open.")
                            continue
                        success = place_simple_market_order(api, trade, capital_per_trade_usd)
                        if success:
                            open_positions.add(trade['ticker'])
                            logging.info(f"Added {trade['ticker']} to in-memory positions to prevent duplicate trades this cycle.")
                            time.sleep(2)
                else:
                    logging.info("No new, unseen trades found since last check.")
            
            trade_history_df = load_trade_history()
            if not trade_history_df.empty:
                check_and_manage_positions(api, trade_history_df)
        
        except Exception as e:
            logging.critical(f"A critical error occurred in the main loop: {e}")

        logging.info(f"Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(60 * CHECK_INTERVAL_MINUTES)