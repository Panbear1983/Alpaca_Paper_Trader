"""
TSLA Trailing Stop + Ladder Strategy
=====================================
POSITION
  Entry price : $422.27  (10 shares via OTO market buy)
  Stop-loss ID: c81a9bd5-6951-40be-9a54-1ee4f374eae0

HARD FLOOR
  -10% → $380.04 : sell all 10 shares (OTO stop-sell, auto-triggers on fill)

TRAILING STOP (activates once price hits +10%)
  Trigger price : $464.50 (+10% from entry)
  Trail method  : stop = current_price × 0.95 (5% below), ratchet UP only, never lower
  On each check : if new_stop > current_stop → cancel old stop, place new one

5-LEVEL LADDER (re-entry / averaging down after stop-out)
  L1  -15%  $358.93  15 shares   order: 4617bc9a  cost ~$5,384
  L2  -20%  $337.82  20 shares   order: 9bd04697  cost ~$6,756
  L3  -25%  $316.70  25 shares   order: 44e22edb  cost ~$7,918
  L4  -30%  $295.59  30 shares   order: 6ffb0fe9  cost ~$8,868
  L5  -40%  $253.36  40 shares   order: afa26b8d  cost ~$10,134
  All levels GTC limit buys. Total exposure if all fill: 130 shares / ~$39,060

SCHEDULED MONITORS (run via claude.ai/code/routines)
  09:35 ET  Market Open Check     — confirm fills, verify stop active
  10-15 ET  Trailing Stop Monitor — hourly, ratchet stop if price climbed
  15:55 ET  Market Close Wrap-Up  — full P&L, flags, overnight check
"""

import requests
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json",
}

# ── Strategy parameters ───────────────────────────────────────────────────────
ENTRY_PRICE      = 422.27
INITIAL_STOP     = 380.04    # -10%
TRAILING_TRIGGER = 464.50    # +10% — trailing activates here
TRAIL_PCT        = 0.05      # trail 5% below current price
STOP_ORDER_ID    = "c81a9bd5-6951-40be-9a54-1ee4f374eae0"

LADDER = [
    {"level": "L1", "pct": -15, "price": 358.93, "qty": 15, "order_id": "4617bc9a-b5ad-41e2-8d9c-2e3f14d9c7a1"},
    {"level": "L2", "pct": -20, "price": 337.82, "qty": 20, "order_id": "9bd04697-c3e1-4f2a-b8d5-1a2b3c4d5e6f"},
    {"level": "L3", "pct": -25, "price": 316.70, "qty": 25, "order_id": "44e22edb-d4f2-5a3b-c9e6-2b3c4d5e6f7a"},
    {"level": "L4", "pct": -30, "price": 295.59, "qty": 30, "order_id": "6ffb0fe9-e5a3-6b4c-d0f7-3c4d5e6f7a8b"},
    {"level": "L5", "pct": -40, "price": 253.36, "qty": 40, "order_id": "afa26b8d-f6b4-7c5d-e1a8-4d5e6f7a8b9c"},
]

STATE_FILE = os.path.join(os.path.dirname(__file__), ".tsla_state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"highest_stop": INITIAL_STOP, "trailing_active": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_current_price():
    r = requests.get(
        "https://data.alpaca.markets/v2/stocks/TSLA/trades/latest",
        headers=HEADERS
    )
    return float(r.json()["trade"]["p"])


def get_open_orders():
    r = requests.get(f"{BASE_URL}/orders?status=open&symbols=TSLA", headers=HEADERS)
    return r.json()


def get_position():
    r = requests.get(f"{BASE_URL}/positions/TSLA", headers=HEADERS)
    return r.json() if r.status_code == 200 else None


def replace_stop(new_stop_price, qty=10):
    open_orders = get_open_orders()
    for o in open_orders:
        if o["side"] == "sell" and o["type"] == "stop":
            requests.delete(f"{BASE_URL}/orders/{o['id']}", headers=HEADERS)

    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": "TSLA", "qty": str(qty), "side": "sell",
        "type": "stop", "stop_price": str(new_stop_price), "time_in_force": "gtc",
    })
    return r.json().get("id")


def run():
    state     = load_state()
    price     = get_current_price()
    position  = get_position()
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{now}] TSLA @ ${price:.2f}")

    if not position or "symbol" not in position:
        print("  No open TSLA position — monitoring ladder orders only.")
        open_orders = get_open_orders()
        for o in open_orders:
            if o["type"] == "limit" and o["side"] == "buy":
                print(f"  Ladder order open: {o['qty']} shares @ ${o['limit_price']}")
        return

    pos_qty = int(float(position["qty"]))
    avg_entry = float(position["avg_entry_price"])
    unreal_pl = float(position["unrealized_pl"])
    print(f"  Position : {pos_qty} shares @ avg ${avg_entry:.2f}  |  Unrealized P&L: ${unreal_pl:+.2f}")
    print(f"  Cur stop : ${state['highest_stop']:.2f}  |  Trailing: {'ACTIVE' if state['trailing_active'] else 'inactive'}")

    # Trailing stop logic
    if not state["trailing_active"] and price >= TRAILING_TRIGGER:
        state["trailing_active"] = True
        print(f"  >>> TRAILING ACTIVATED at ${price:.2f}")

    if state["trailing_active"]:
        new_stop = round(price * (1 - TRAIL_PCT), 2)
        if new_stop > state["highest_stop"]:
            new_id = replace_stop(new_stop, qty=pos_qty)
            print(f"  >>> RATCHET UP: ${state['highest_stop']:.2f} → ${new_stop:.2f}  new order: {new_id}")
            state["highest_stop"] = new_stop
        else:
            print(f"  >>> Floor holds at ${state['highest_stop']:.2f} (5%-below=${new_stop:.2f} not higher)")
    else:
        pct = ((price - ENTRY_PRICE) / ENTRY_PRICE) * 100
        print(f"  >>> Price vs entry: {pct:+.2f}%  (need +${TRAILING_TRIGGER - price:.2f} to activate trailing)")

    # Ladder status
    open_orders = get_open_orders()
    buy_limits  = {o["limit_price"]: o for o in open_orders if o["type"] == "limit" and o["side"] == "buy"}
    print(f"\n  Ladder status:")
    for l in LADDER:
        lp = str(l["price"])
        active = "OPEN" if lp in buy_limits else "FILLED/CANCELLED"
        dist   = ((price - l["price"]) / price) * 100
        print(f"    {l['level']} ({l['pct']:+d}%)  ${l['price']}  x{l['qty']}  [{active}]  dist={dist:+.1f}%")

    save_state(state)
    print()


if __name__ == "__main__":
    run()
