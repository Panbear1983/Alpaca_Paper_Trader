import os
from dotenv import load_dotenv
load_dotenv()  # loads .env file in current directory
from alpaca_trade_api import REST

# Set environment variables for the Alpaca API
os.environ['APCA_API_KEY_ID'] = os.getenv('ALPACA_API_KEY')
os.environ['APCA_API_SECRET_KEY'] = os.getenv('ALPACA_SECRET_KEY')
os.environ['APCA_API_BASE_URL'] = os.getenv('ALPACA_BASE_URL')

api = REST()
account = api.get_account()
print('EQUITY:', account.equity)
print('LAST_EQUITY:', account.last_equity)
day_pl = float(account.equity) - float(account.last_equity)
print('DAY_PL:', day_pl)
print('DAY_PL_%:', (day_pl / float(account.last_equity)) * 100 if float(account.last_equity) != 0 else 0)
print('CASH:', account.cash)
print('BUYING_POWER:', account.buying_power)
print()
print('POSITIONS:')
positions = api.list_positions()
for p in positions:
    print(f'  {p.symbol}: {float(p.qty):.4f} shares at {float(p.avg_entry_price):.2f} (market_value: ${float(p.market_value):.2f})')
print()
print('OPEN_ORDERS:')
orders = api.list_orders(status='open')
for o in orders:
    print(f'  {o.id}: {o.side} {o.qty} {o.type} {o.symbol} at {o.limit_price or o.stop_price} (status: {o.status})')
