import os
from dotenv import load_dotenv
load_dotenv()
from alpaca_trade_api import REST

key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base = os.getenv('ALPACA_BASE_URL')

api = REST(key_id=key, secret_key=secret, base_url=base)

# Get SH position
try:
    position = api.get_position('SH')
    print(f'SH position: {position.qty} shares at {position.avg_entry_price}')
except Exception as e:
    print(f'No SH position: {e}')
    position = None

# Get open orders for SH
orders = api.list_orders(status='open', symbols=['SH'])
print(f'Open orders for SH: {len(orders)}')
for order in orders:
    print(f'Order {order.id}: {order.side} {order.qty} {order.type} at {order.limit_price or order.stop_price}')

# Cancel open orders for SH
for order in orders:
    try:
        api.cancel_order(order.id)
        print(f'Canceled order {order.id}')
    except Exception as e:
        print(f'Failed to cancel order {order.id}: {e}')

# Check position again and sell if exists
try:
    position = api.get_position('SH')
    if float(position.qty) > 0:
        print(f'Selling {position.qty} shares of SH at market')
        api.submit_order(
            symbol='SH',
            qty=position.qty,
            side='sell',
            type='market',
            time_in_force='day'
        )
        print('Market sell order submitted for SH')
    else:
        print('No SH position to sell')
except Exception as e:
    print(f'Error selling SH: {e}')
