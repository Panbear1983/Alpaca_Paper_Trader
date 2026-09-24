import os
from alpaca_trade_api.rest import REST
import datetime

# Setup
api = REST()

# Account
account = api.get_account()
equity = float(account.equity)
last_equity = float(account.last_equity)
today_pl = equity - last_equity
today_pl_pct = today_pl / last_equity * 100 if last_equity != 0 else 0

print(f"Equity: ${equity:.2f}")
print(f"Last equity: ${last_equity:.2f}")
print(f"Today's P&L: ${today_pl:.2f} ({today_pl_pct:.2f}%)")
print()

# Positions
positions = api.list_positions()
print(f"Current positions ({len(positions)}):")
total_market_value = 0.0
for p in positions:
    qty = float(p.qty)
    avg_entry = float(p.avg_entry_price)
    price = float(p.current_price)
    mv = float(p.market_value)
    total_market_value += mv
    print(f"  {p.symbol}: qty={qty:.4f}, avg_entry=${avg_entry:.2f}, "
          f"price=${price:.2f}, market_value=${mv:.2f}, "
          f"unrealized PL%={float(p.unrealized_plpc)*100:.2f}%")
print(f"Total market value: ${total_market_value:.2f}")
print()

# Determine date for the last trading day (yesterday if market is closed, else today?)
clock = api.get_clock()
print(f"Market open: {clock.is_open}, next open: {clock.next_open}, next close: {clock.next_close}")
print()

from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
# We'll get orders from the last 2 days
start = now - timedelta(days=2)
orders = api.list_orders(status='all', limit=500, after=start.isoformat())
print(f"Total orders in last 2 days: {len(orders)}")
# Filter orders with filled_at not None and side in ['buy','sell']
filled_orders = [o for o in orders if o.filled_at is not None and o.side in ['buy','sell']]
print(f"Filled orders in last 2 days: {len(filled_orders)}")
# Group by date (convert filled_at to date)
from collections import defaultdict
orders_by_date = defaultdict(list)
for o in filled_orders:
    # filled_at is a string like '2026-08-26T14:30:00Z'
    dt = datetime.fromisoformat(o.filled_at.replace('Z', '+00:00'))
    date_str = dt.date().isoformat()
    orders_by_date[date_str].append(o)
# Determine the most recent date with orders (likely the last trading day)
if orders_by_date:
    latest_date = max(orders_by_date.keys())
    print(f"\nFilled orders on {latest_date}:")
    for o in orders_by_date[latest_date]:
        print(f"  {o.id} {o.symbol} {o.side} {o.qty} @ {o.filled_avg_price} {o.type} {o.time_in_force} filled_at={o.filled_at}")
else:
    print("No filled orders found in last 2 days.")

# Also check for any stop orders that might have been triggered (stop loss)
stop_orders = [o for o in filled_orders if o.type in ['stop','stop_limit']]
if stop_orders:
    print(f"\nStop orders filled: {len(stop_orders)}")
    for o in stop_orders:
        print(f"  {o.id} {o.symbol} {o.side} {o.qty} @ {o.filled_avg_price} {o.type}")
else:
    print("\nNo stop orders filled.")

