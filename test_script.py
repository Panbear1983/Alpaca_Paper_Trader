import os
os.environ['APCA_API_KEY_ID']='PKOKGBMME3EHLI7SDI4V6LF4BX'
os.environ['APCA_API_SECRET_KEY']='FxcK7zYovrHGK8H31JopxdQsiddwRSBzGDAraGsfK42t'
os.environ['APCA_API_BASE_URL']='https://paper-api.alpaca.markets'
from alpaca_trade_api import REST
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
