import requests
import json
import os

#url = "https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL&timeframe=1H&limit=1000&adjustment=raw&feed=sip&sort=asc"
#https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL&timeframe=1H&start=2024-01-03T00%3A00%3A00Z&end=2024-01-04T00%3A00%3A00Z&limit=1000&adjustment=raw&feed=sip&sort=asc"
headers = {
    "accept": "application/json",
    "APCA-API-KEY-ID": "PKDBS69E76P0DIEJF5AR",
    "APCA-API-SECRET-KEY": "9xnzc0fSSfduish5ocaHIYkOpaBapZChTSX2AqRf"
}
symbol = "AAPL"
timeframe = "1H"
url = "https://data.alpaca.markets/v2/stocks/bars?symbols="+symbol+"&timeframe="+timeframe+"&start=2024-01-03T00%3A00%3A00Z&end=2024-01-04T00%3A00%3A00Z&limit=1000&adjustment=raw&feed=sip&sort=asc"

response = requests.get(url, headers=headers)

print(response.text)





def get_historical_data_direct(symbol, timeframe, start_date, end_date):
    """
    Fetches historical data directly from the Alpaca API using requests.
    - symbol: The stock ticker (e.g., "AAPL")
    - timeframe: The data resolution (e.g., "1H", "1Day")
    - start_date, end_date: ISO 8601 format strings (e.g., "2024-01-03T00:00:00Z")
    """
    # Check if keys are loaded
    if 0:
        print("Error: Please set your ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")
        return

    # Endpoint URL for historical bars
    url = "https://data.alpaca.markets/v2/stocks/bars"

    # Set up the headers with your new, secure keys
    headers = {
        "accept": "application/json",
        "APCA-API-KEY-ID": "PKDBS69E76P0DIEJF5AR",
        "APCA-API-SECRET-KEY": "9xnzc0fSSfduish5ocaHIYkOpaBapZChTSX2AqRf"
    }

    # Set up the parameters for the API request
    params = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": start_date,
        "end": end_date,
        "limit": 1000,
        "adjustment": "raw",
        "feed": "sip", # Use 'sip' for paid/pro data, 'iex' for free data
        "sort": "asc"
    }

    try:
        # Make the GET request to the Alpaca API
        response = requests.get(url, headers=headers, params=params)

        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status()

        # Parse the JSON response into a Python dictionary
        data = response.json()

        # Pretty-print the JSON data
        print(f"--- Successfully fetched data for {symbol} ---")
        print(json.dumps(data, indent=4))

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
        print(f"Response content: {response.text}")
    except Exception as e:
        print(f"An other error occurred: {e}")


# --- Example Usage ---
# Use your NEW keys after setting them as environment variables
get_historical_data_direct(
    symbol="AAPL",
    timeframe="1H",
    start_date="2024-01-03T00:00:00Z",
    end_date="2024-01-04T00:00:00Z"
)