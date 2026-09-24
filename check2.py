import os
from alpaca_trade_api import REST

# Set the environment variables that alpaca_trade_api expects
os.environ['APCA_API_KEY_ID'] = os.getenv('ALPACA_API_KEY')
os.environ['APCA_API_SECRET_KEY'] = os.getenv('ALPACA_SECRET_KEY')
os.environ['APCA_API_BASE_URL'] = os.getenv('ALPACA_BASE_URL')

api = REST()
account = api.get_account()
print('Account Equity:', account.equity)
print('Cash:', account.cash)
print('Buying Power:', account.buying_power)
print('Last Equity:', account.last_equity)
today_pl = float(account.equity) - float(account.last_equity)
print("Today's P&L: ${:.2f}".format(today_pl))
positions = api.list_positions()
print('\nPositions:')
if positions:
    for p in positions:
        print(f'{p.symbol}: {p.qty} shares, Market Value: ${float(p.market_value):.2f}, Cost Basis: ${float(p.cost_basis):.2f}, Unrealized P/L: ${float(p.unrealized_pl):.2f}')
else:
    print('No open positions')
# Check the SH order from script output
order_id = '440268b0-5cb4-47e0-b1c9-b2caf9cfa613'
try:
    order = api.get_order(order_id)
    print(f'\nSH Order {order_id}: {order.status}, filled at {order.filled_avg_price} qty {order.filled_qty}')
except Exception as e:
    print(f'\nError fetching order: {e}')
