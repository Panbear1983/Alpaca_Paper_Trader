import os
from dotenv import load_dotenv
load_dotenv()
from alpaca_trade_api.rest import REST
key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base = os.getenv('ALPACA_BASE_URL')
api = REST(key, secret, base, api_version='v2')
# Cancel open orders for SH
print('Checking for open SH orders...')
orders = api.list_orders(status='open', symbols=['SH'])
for o in orders:
    print(f'Cancelling order {o.id} ({o.side} {o.qty} {o.type})')
    api.cancel_order(o.id)
# Get SH position
try:
    pos = api.get_position('SH')
    print(f'SH position: {pos.qty} shares, market value: ${float(pos.market_value):.2f}')
    if float(pos.qty) > 0:
        print(f'Submitting market sell order for {pos.qty} SH')
        order = api.submit_order(
            symbol='SH',
            qty=pos.qty,
            side='sell',
            type='market',
            time_in_force='day'
        )
        print(f'Submitted order {order.id}')
    else:
        print('No SH position to sell (quantity <= 0)')
except Exception as e:
    print(f'No SH position: {e}')
