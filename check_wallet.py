import os
import sys
from alpaca_trade_api import REST
from dotenv import load_dotenv

# Load environment variables from the .env file in this directory
load_dotenv('.env')

# Get Alpaca credentials
api_key = os.getenv('ALPACA_API_KEY')
secret_key = os.getenv('ALPACA_SECRET_KEY')
base_url = os.getenv('ALPACA_BASE_URL')

# Initialize API
api = REST(api_key, secret_key, base_url)

# Get account info
account = api.get_account()
equity = float(account.equity)
last_equity = float(account.last_equity)
day_pnl = equity - last_equity
day_pnl_pct = (day_pnl / last_equity) * 100 if last_equity != 0 else 0

print(f"Account Status: {account.status}")
print(f"Equity: ${equity:,.2f}")
print(f"Last Equity (prev close): ${last_equity:,.2f}")
print(f"Day P&L: ${day_pnl:,.2f} ({day_pnl_pct:+.2f}%)")
print(f"Cash: ${float(account.cash):,.2f}")
print(f"Buying Power: ${float(account.buying_power):,.2f}")

# Get positions
positions = api.list_positions()
print(f"\nNumber of Positions: {len(positions)}")
for pos in positions:
    print(f"{pos.symbol}: {pos.qty} shares @ ${float(pos.avg_entry_price):.2f} (Current: ${float(pos.current_price):.2f}) P&L: ${float(pos.unrealized_pl):,.2f} ({float(pos.unrealized_plpc)*100:+.2f}%)")

# Check for SH specifically
try:
    sh_pos = api.get_position('SH')
    print(f"\nSH Position: {sh_pos.qty} shares @ ${float(sh_pos.avg_entry_price):.2f} (Current: ${float(sh_pos.current_price):.2f}) P&L: ${float(sh_pos.unrealized_pl):,.2f} ({float(sh_pos.unrealized_plpc)*100:+.2f}%)")
except Exception as e:
    print(f"\nNo SH position found: {e}")

# Check recent orders for SH (today)
try:
    orders = api.list_orders(status='all', limit=50, after='2026-08-27')  # today's date in YYYY-MM-DD
    sh_orders = [o for o in orders if o.symbol == 'SH']
    if sh_orders:
        print(f"\nRecent SH Orders:")
        for o in sh_orders:
            print(f"  ID: {o.id}, Side: {o.side}, Qty: {o.qty}, Price: ${float(o.limit_price) if o.limit_price else 'Market'}, Status: {o.status}, Filled Avg Price: ${float(o.filled_avg_price) if o.filled_avg_price else 'N/A'}")
    else:
        print("\nNo SH orders found today.")
except Exception as e:
    print(f"\nError fetching orders: {e}")
