import sys
sys.path.insert(0, '.')
from capitol_copier import ALPACA_HEADERS, BASE_URL
import requests
import time

def get_position(symbol):
    url = f'{BASE_URL}/positions/{symbol}'
    resp = requests.get(url, headers=ALPACA_HEADERS)
    if resp.status_code == 200:
        return resp.json()
    elif resp.status_code == 404:
        return None
    else:
        print(f'Error fetching position for {symbol}: {symbol}: {resp.status_code} {resp.text}')
        return None

print('=== Current SH position ===')
pos = get_position('SH')
if pos:
    qty = float(pos.get('qty', 0))
    print(f'SH position: qty={qty}, market_value=${float(pos.get("market_value", 0)):.2f}')
else:
    print('No SH position')

print('\n=== Checking recent orders ===')
# Get recent orders
url = f'{BASE_URL}/orders'
params = {'status': 'filled', 'symbol': 'SH', 'limit': 5}
resp = requests.get(url, headers=ALPACA_HEADERS, params=params)
if resp.status_code == 200:
    orders = resp.json()
    print(f'Found {len(orders)} filled SH orders')
    for o in orders:
        print(f'  Order {o["id"]}: side={o["side"]}, qty={o["filled_qty"]}, filled_avg_price={o["filled_avg_price"]}')
else:
    print(f'Error getting filled orders: {resp.status_code} {resp.text}')
