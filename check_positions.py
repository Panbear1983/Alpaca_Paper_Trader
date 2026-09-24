import os
from alpaca_trade_api.rest import REST
api_key = os.getenv('ALPACA_API_KEY')
api_secret = os.getenv('ALPACA_SECRET_KEY')
base_url = os.getenv('ALPACA_BASE_URL')
client = REST(api_key, api_secret, base_url, api_version='v2')
account = client.get_account()
print('High Risk Wallet:')
print(f'  Equity: ${float(account.equity):,.2f}')
print(f'  Cash: ${float(account.cash):,.2f}')
print(f'  Buying Power: ${float(account.buying_power):,.2f}')
# Try to get daytrade_count if exists
if hasattr(account, 'daytrade_count'):
    print(f'  Daytrade Count: {account.daytrade_count}')
else:
    print('  Daytrade Count: N/A')
print()
# Get positions
positions = client.list_positions()
print(f'Positions ({len(positions)}):')
for p in positions:
    print(f'  {p.symbol}: {p.qty} shares @ ${float(p.avg_entry_price):.2f} (${float(p.market_value):,.2f})')
# Get orders today
from datetime import datetime, timezone
today = datetime.now(timezone.utc).date()
orders = client.list_orders(status='all', limit=500)
filled_today = []
for o in orders:
    if o.filled_at:
        # Alpaca returns ISO string with Z or +00:00
        filled_str = o.filled_at
        if filled_str.endswith('Z'):
            filled_str = filled_str[:-1] + '+00:00'
        filled_time = datetime.fromisoformat(filled_str)
        if filled_time.date() == today:
            filled_today.append(o)
print(f'\nFilled orders today: {len(filled_today)}')
for o in filled_today:
    print(f'  {o.side} {o.qty} {o.symbol} @ {o.filled_avg_price} ({o.id})')
