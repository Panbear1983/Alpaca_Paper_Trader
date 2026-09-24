import os
from alpaca_trade_api import REST
from datetime import datetime, timedelta
import pytz

# Load .env
env_path = '.env'
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k] = v

api = REST(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), os.getenv('ALPACA_BASE_URL'))

account = api.get_account()
print('EQUITY: ${:,.2f}'.format(float(account.equity)))
print('LAST EQUITY: ${:,.2f}'.format(float(account.last_equity)))
day_pnl = float(account.equity) - float(account.last_equity)
print('DAY P&L (equity - last_equity): ${:,.2f}'.format(day_pnl))
print('CASH: ${:,.2f}'.format(float(account.cash)))
print('BUYING POWER: ${:,.2f}'.format(float(account.buying_power)))
print()

print('POSITIONS:')
positions = api.list_positions()
total_unrealized = 0.0
for p in positions:
    qty = float(p.qty)
    avg = float(p.avg_entry_price)
    price = float(p.current_price)
    pl = (price - avg) * qty
    total_unrealized += pl
    print('  {:<6} {:>8} shares @ {:>8.2f} -> {:>8.2f} (P&L: {:>8.2f}, {:>6.2f}%)'.format(p.symbol, qty, avg, price, pl, (pl/(avg*qty))*100 if avg*qty != 0 else 0))
print('TOTAL UNREALIZED P&L: {:,.2f}'.format(total_unrealized))
print()

# Get yesterday's date in ET
et = pytz.timezone('US/Eastern')
today = datetime.now(et).date()
yesterday = today - timedelta(days=1)

# Start and end of yesterday in ET
start = et.localize(datetime(yesterday.year, yesterday.month, yesterday.day))
end = start + timedelta(days=1)

print('ORDERS FOR {}:'.format(yesterday.isoformat()))
orders = api.list_orders(status='all', after=start.isoformat(), until=end.isoformat(), limit=500)
filled = []
for o in orders:
    if hasattr(o, 'filled_qty') and o.filled_qty is not None:
        try:
            fq = float(o.filled_qty)
            if fq > 0:
                filled.append(o)
        except:
            pass
print('Total orders: {}'.format(len(orders)))
print('Filled orders: {}'.format(len(filled)))
for o in filled:
    # Try to get the filled average price and the timestamp
    filled_at = getattr(o, 'filled_at', None)
    if filled_at:
        time_str = filled_at.strftime('%H:%M:%S')
    else:
        time_str = 'N/A'
    print('  {} {} {} @ {} ({}) filled at {}'.format(o.created_at.strftime('%H:%M:%S'), o.symbol, o.side, o.filled_avg_price, o.status, time_str))
