import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv()  # loads .env file
API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_SECRET_KEY')
BASE_URL = os.getenv('ALPACA_BASE_URL')
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
account = api.get_account()
equity = float(account.equity)
last_equity = float(account.last_equity)
today_pl = equity - last_equity
open_equity = last_equity
daily_loss_limit = 0.03 * open_equity
print('EQUITY: ${:,.2f}'.format(equity))
print('TODAY P/L: {:,.2f} ({:+.2f}%)'.format(today_pl, today_pl/open_equity*100))
print('DAILY LOSS LIMIT (3% of open equity): ${:,.2f}'.format(daily_loss_limit))
print()
print('POSITIONS:')
positions = api.list_positions()
for p in positions:
    print('{:6} qty {:>10.6f} market value {:>10.2f} avg entry {:>8.2f} unrealized pl {:>8.2f}'.format(p.symbol, float(p.qty), float(p.market_value), float(p.avg_entry_price), float(p.unrealized_pl)))
print()
print('OPEN ORDERS:')
orders = api.list_orders(status='open', limit=50)
for o in orders:
    print('{:.8} {:6} {:4} {:>10.6f} {:>10} {} {}'.format(o.id[:8], o.symbol, o.side, float(o.qty), o.order_type, o.status, o.submitted_at.strftime('%Y-%m-%d %H:%M:%S')))
