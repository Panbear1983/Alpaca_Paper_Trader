import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from alpaca_trade_api.rest import REST
import dateutil.parser

# Use the environment variables as defined in .env
key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base = os.getenv('ALPACA_BASE_URL')
if not key or not secret:
    raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment")
api = REST(key, secret, base, api_version='v2')

# Account info
account = api.get_account()
equity = float(account.equity)
cash = float(account.cash)
buying_power = float(account.buying_power)
multiplier = float(account.multiplier)
print(f"Account Equity: ${equity:,.2f}")
print(f"Cash: ${cash:,.2f}")
print(f"Buying Power: ${buying_power:,.2f}")
print(f"Multiplier: {multiplier}")

# Positions
positions = api.list_positions()
print(f"\nPositions ({len(positions)}):")
for p in positions:
    print(f"  {p.symbol}: {p.qty} shares, market value: ${float(p.market_value):,.2f}, cost basis: ${float(p.cost_basis):,.2f}, unrealized PL: ${float(p.unrealized_pl):,.2f}")

# Open orders
open_orders = api.list_orders(status='open')
print(f"\nOpen orders ({len(open_orders)}):")
for o in open_orders:
    print(f"  {o.id} {o.symbol} {o.side} {o.qty} {o.type} {o.status} at {o.submitted_at}")

# Yesterday's date in ET
clock = api.get_clock()
today_et = clock.timestamp.date()
yesterday_et = today_et - timedelta(days=1)
print(f"\nToday (ET): {today_et}")
print(f"Yesterday (ET): {yesterday_et}")

# Get all orders (limit 500) and filter by filled_at date
orders = api.list_orders(status='all', limit=500)
filled_yesterday = []
for o in orders:
    if o.filled_at:
        # o.filled_at might be a string or a Timestamp
        if isinstance(o.filled_at, str):
            filled_str = o.filled_at
        else:
            # Assume it's a datetime-like object, convert to ISO string
            filled_str = o.filled_at.isoformat()
        try:
            filled_date = dateutil.parser.parse(filled_str).date()
        except Exception:
            # If parsing fails, skip
            continue
        if filled_date == yesterday_et:
            filled_yesterday.append(o)
print(f"\nOrders filled yesterday: {len(filled_yesterday)}")
if filled_yesterday:
    # Show first 10
    for o in filled_yesterday[:10]:
        print(f"  {o.id} {o.symbol} {o.side} {o.filled_qty} @ {o.filled_avg_price} {o.type} {o.status}")
    if len(filled_yesterday) > 10:
        print(f"  ... and {len(filled_yesterday)-10} more")

# Performance log
try:
    with open('performance_log.json') as f:
        data = json.load(f)
    trades = data.get('trades', [])
    print(f"\nPerformance log: {len(trades)} trades recorded")
    if trades:
        gross_profit = 0.0
        gross_loss = 0.0
        wins = 0
        losses = 0
        total_hold_days = 0
        for t in trades:
            pnl = (t['exit_price'] - t['entry_price']) * t['qty']
            if pnl > 0:
                gross_profit += pnl
                wins += 1
            else:
                gross_loss += abs(pnl)
                losses += 1
            total_hold_days += t.get('hold_days', 0)
        win_rate = wins / len(trades) if trades else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        avg_hold = total_hold_days / len(trades) if trades else 0
        print(f"  Wins: {wins}, Losses: {losses}, Win rate: {win_rate:.2%}")
        print(f"  Gross profit: ${gross_profit:,.2f}")
        print(f"  Gross loss: ${gross_loss:,.2f}")
        print(f"  Profit factor: {profit_factor:.2f}")
        print(f"  Average hold days: {avg_hold:.1f}")
        # Trades exited yesterday
        exits_yesterday = [t for t in trades if t.get('exit_date') == str(yesterday_et)]
        print(f"  Trades exited yesterday: {len(exits_yesterday)}")
        if exits_yesterday:
            gross_yesterday = sum((t['exit_price'] - t['entry_price']) * t['qty'] for t in exits_yesterday)
            print(f"  Gross P&L from trades exited yesterday: ${gross_yesterday:,.2f}")
except Exception as e:
    print(f"\nError reading performance log: {e}")

# Check for any SH position (already covered in positions)
# Check for any VFS position
print("\n--- End of report ---")
