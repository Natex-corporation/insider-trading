import datetime
import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup
import os
import time
import json
import re
from typing import Optional, Dict
from alpaca_trade_api.rest import REST, APIError

# --- Basic Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

# --- ALPACA API CONFIGURATION (Using hardcoded keys as requested for testing) ---
# !!! WARNING: These keys are exposed and should be revoked after testing.
API_KEY = "PKDBS69E76P0DIEJF5AR"
SECRET_KEY = "9xnzc0fSSfduish5ocaHIYkOpaBapZChTSX2AqRf"
BASE_URL = "https://paper-api.alpaca.markets"

# --- State Management & Parameters ---
TRADE_CAPITAL_CZK = 250.0
TAKE_PROFIT_PERCENT = 10.0
CHECK_INTERVAL_MINUTES = 15


RATE_CACHE_FILE = "czk_usd_rate.cache"

def get_usd_per_czk() -> float:
    """Return the CZK→USD exchange rate.

    Tries a primary API, then a backup provider. If both fail, it falls back to
    a cached value on disk or a conservative default so the bot can continue
    operating even without network access.
    """
    urls = [
        "https://api.exchangerate-api.com/v4/latest/CZK",
        "https://open.er-api.com/v6/latest/CZK",
    ]
    rate: Optional[float] = None
    for url in urls:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            rate = float(data["rates"]["USD"])
            logging.info(f"Fetched CZK to USD exchange rate: {rate}")
            break
        except Exception as e:
            logging.warning(f"Exchange rate fetch failed from {url}: {e}")

    if rate is None:
        try:
            with open(RATE_CACHE_FILE, "r") as f:
                rate = float(f.read().strip())
            logging.warning(f"Using cached CZK to USD exchange rate: {rate}")
        except Exception:
            rate = 0.045  # conservative fallback
            logging.warning(
                f"Using fallback CZK to USD exchange rate: {rate} (no cache available)"
            )

    else:
        try:
            with open(RATE_CACHE_FILE, "w") as f:
                f.write(str(rate))
        except Exception as e:
            logging.debug(f"Could not write rate cache: {e}")

    return rate

def parse_finviz_date(raw: str) -> Optional[datetime.date]:
    # This function is from your script and remains unchanged.
    try: return datetime.datetime.strptime(raw.strip(), "%b %d '%y").date()
    except ValueError:
        try:
            dt = datetime.datetime.strptime(raw.strip(), "%b %d").date()
            return dt.replace(year=datetime.date.today().year)
        except Exception: return None

def infer_direction(txn: str) -> Optional[str]:
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

def get_current_positions(api: REST) -> Dict[str, str]:
    """Return a mapping of ticker symbol to position side."""
    try:
        positions = api.list_positions()
        return {
            p.symbol: p.side
            for p in positions
            if float(getattr(p, "qty_available", p.qty)) > 0
        }
    except Exception as e:
        logging.error(f"Could not list open positions: {e}")
        return {}

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
    else:
        tp_price = round(latest_price * (1 - TAKE_PROFIT_PERCENT / 100), 2)
    sl_price = None
    
    logging.info(f"Submitting {side} MARKET order for {qty} shares of {symbol}.")
    try:
        # --- PLAN A: Submit the order as calculated ---
        order = api.submit_order(symbol=symbol, qty=qty, side=side, type='market', time_in_force='day')
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

def check_and_manage_positions(api: REST):
    """Close positions once their take-profit level is reached."""
    logging.info("Checking open positions for exit signals...")
    try:
        positions = api.list_positions()
    except Exception as e:
        logging.error(f"Could not list open positions: {e}")
        return
    if not positions:
        logging.info("No open positions to manage.")
        return
    for pos in positions:
        try:
            current_price = api.get_latest_trade(pos.symbol).price
            entry = float(pos.avg_entry_price)
            tp_price = (
                entry * (1 + TAKE_PROFIT_PERCENT / 100)
                if pos.side == 'long'
                else entry * (1 - TAKE_PROFIT_PERCENT / 100)
            )
            exit_reason = None
            if pos.side == 'long' and current_price >= tp_price:
                exit_reason = f"take profit at ${tp_price:.2f}"
            elif pos.side == 'short' and current_price <= tp_price:
                exit_reason = f"take profit at ${tp_price:.2f}"
            if exit_reason:
                qty_avail = float(getattr(pos, "qty_available", pos.qty))
                if qty_avail <= 0:
                    logging.warning(
                        f"Skipping close for {pos.symbol}: no available quantity (qty_available={qty_avail})."
                    )
                    continue
                logging.info(f"Closing {pos.symbol} due to {exit_reason}.")
                api.close_position(pos.symbol)
        except Exception as e:
            logging.error(f"Error managing position for {pos.symbol}: {e}")

# --------------------------------------------------
# 3. Main Execution Loop
# --------------------------------------------------
if __name__ == '__main__':
    api = REST(key_id=API_KEY, secret_key=SECRET_KEY, base_url=BASE_URL)
    logging.info("--- Starting Continuous Insider Trading Bot with Smart Error Handling ---")
    
    while True:
        try:
            usd_per_czk = get_usd_per_czk()
            capital_per_trade_usd = TRADE_CAPITAL_CZK * usd_per_czk
            logging.info(f"{TRADE_CAPITAL_CZK} CZK is approx ${capital_per_trade_usd:.2f} USD.")

            positions = get_current_positions(api)
            logging.info(f"Holding {len(positions)} positions: {list(positions.keys())}")

            latest_trades_df = fetch_insider_trades()
            if not latest_trades_df.empty:
                for _, trade in latest_trades_df.iterrows():
                    existing_side = positions.get(trade['ticker'])
                    desired_side = 'long' if trade['direction'] == 'buy' else 'short'
                    if existing_side == desired_side:
                        logging.info(f"Skipping trade for {trade['ticker']}: {existing_side} position already open.")
                        continue
                    if existing_side and existing_side != desired_side:
                        logging.info(f"Closing existing {existing_side} position in {trade['ticker']} before opening {desired_side}.")
                        try:
                            api.close_position(trade['ticker'])
                            positions.pop(trade['ticker'], None)
                        except Exception as e:
                            logging.error(f"Failed to close position for {trade['ticker']}: {e}")
                            continue
                    success = place_simple_market_order(api, trade, capital_per_trade_usd)
                    if success:
                        positions[trade['ticker']] = desired_side
                        logging.info(f"Added {trade['ticker']} to in-memory positions to prevent duplicate trades this cycle.")
                        time.sleep(2)

            check_and_manage_positions(api)
        
        except Exception as e:
            logging.critical(f"A critical error occurred in the main loop: {e}")

        logging.info(f"Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(60 * CHECK_INTERVAL_MINUTES)
