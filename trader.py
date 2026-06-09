import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json",
}


def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def place_order(symbol, qty, side, order_type="market", time_in_force="day"):
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def get_orders(status="open"):
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params={"status": status})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    account = get_account()
    print(f"Account status : {account['status']}")
    print(f"Buying power   : ${float(account['buying_power']):,.2f}")
    print(f"Portfolio value: ${float(account['portfolio_value']):,.2f}")

    positions = get_positions()
    if positions:
        print("\nOpen positions:")
        for p in positions:
            print(f"  {p['symbol']}: {p['qty']} shares @ avg ${float(p['avg_entry_price']):,.2f}")
    else:
        print("\nNo open positions.")
