import os
from dotenv import load_dotenv
load_dotenv()
from alpaca_trade_api.rest import REST
key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base = os.getenv('ALPACA_BASE_URL')
api = REST(key, secret, base, api_version='v2')
order_id = '51b32210-da53-44ca-9c8f-2971e2f8b7e4'
try:
    order = api.get_order(order_id)
    print(f'Order {order_id}: {order.status} {order.side} {order.qty} {order.filled_qty} {order.filled_avg_price}')
except Exception as e:
    print(f'Error fetching order: {e}')
# Check SH position after order
try:
    pos = api.get_position('SH')
    print(f'SH position after: {pos.qty} shares, market value: ${float(pos.market_value):.2f}')
except Exception as e:
    print(f'No SH position: {e}')
