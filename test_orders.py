import json, requests, os
from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")
H_ALPACA = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}

r = requests.get(f"{BASE_URL}/orders", headers=H_ALPACA, params={"status": "filled", "limit": 50, "direction": "desc"}, timeout=10)
orders = r.json()
alpaca_ids = [o["id"] for o in orders]

with open(".event_watcher_state.json") as f:
    state = json.load(f)
state_ids = state.get("last_filled_orders", [])

print("Alpaca IDs count:", len(alpaca_ids))
print("State IDs count:", len(state_ids))
print("Intersection:", len(set(alpaca_ids).intersection(state_ids)))
