import os
from dotenv import load_dotenv
load_dotenv()
from alpaca_trade_api.rest import REST
key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base = os.getenv('ALPACA_BASE_URL')
api = REST(key, secret, base, api_version='v2')
# Check SH position
try:
    pos = api.get_position('SH')
    qty = float(pos.qty)
    market_value = float(pos.market_value)
    cost_basis = float(pos.cost_basis)
    unrealized_pl = float(pos.unrealized_pl)
    print(f'SH position: {qty} shares, market value: ${market_value:.2f}, cost basis: ${cost_basis:.2f}, unrealized PL: ${unrealized_pl:.2f}')
    if qty != 0:
        avg_price = cost_basis / qty
        print(f'Average entry price: ${avg_price:.2f}')
        current_price = market_value / qty if qty != 0 else 0
        print(f'Current price (approx): ${current_price:.2f}')
        change_pct = (current_price - avg_price) / avg_price * 100 if avg_price != 0 else 0
        print(f'Change from entry: {change_pct:.2f}%')
except Exception as e:
    print(f'No SH position: {e}')
    qty = 0
# Check open orders for SH
open_orders = api.list_orders(status='open', symbols=['SH'])
print(f'\nOpen SH orders: {len(open_orders)}')
for o in open_orders:
    print(f'  {o.id} {o.side} {o.qty} {o.type} {o.status}')
# Check recent fills for SH (last 24h)
from datetime import datetime, timedelta
import dateutil.parser
now = datetime.now()
yesterday = now - timedelta(days=1)
orders = api.list_orders(status='all', symbols=['SH'], limit=50)
filled_recent = []
for o in orders:
    if o.filled_at:
        filled_time = dateutil.parser.parse(o.filled_at)
        if filled_time >= yesterday:
            filled_recent.append(o)
print(f'\nSH orders filled in last 24h: {len(filled_recent)}')
for o in filled_recent:
    print(f'  {o.id} {o.side} {o.filled_qty} @ {o.filled_avg_price} {o.type} {o.status} filled at {o.filled_at}')
# Account equity
account = api.get_account()
print(f'\nAccount equity: ${float(account.equity):.2f}')
print(f'Cash: ${float(account.cash):.2f}')
print(f'Buying power: ${float(account.buying_power):.2f}')
