import requests

url = "https://paper-api.alpaca.markets/v2/orders"

payload = {
    "type": "market",
    "time_in_force": "day",
    "symbol": "AAPL",
    "qty": "1",
    "side": "buy"
}
headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "APCA-API-KEY-ID": "PKDBS69E76P0DIEJF5AR",
    "APCA-API-SECRET-KEY": "9xnzc0fSSfduish5ocaHIYkOpaBapZChTSX2AqRf"
}

response = requests.post(url, json=payload, headers=headers)

print(response.text)