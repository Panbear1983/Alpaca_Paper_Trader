import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

os.environ['APCA_API_KEY_ID'] = os.getenv('ALPACA_API_KEY')
os.environ['APCA_API_SECRET_KEY'] = os.getenv('ALPACA_SECRET_KEY')
os.environ['APCA_API_BASE_URL'] = os.getenv('ALPACA_BASE_URL')

from alpaca_trade_api import REST
import datetime

api = REST()

# Get current positions
positions = api.list_positions()

stopped_symbols = []

for p in positions:
    symbol = p.symbol
    qty = float(p.qty)
    avg_entry_price = float(p.avg_entry_price)
    current_price = float(p.market_value) / qty if qty != 0 else 0
    ret = (current_price - avg_entry_price) / avg_entry_price
    if ret <= -0.05:
        print(f"STOP TRIGGERED for {symbol}: entry {avg_entry_price:.2f}, current {current_price:.2f}, ret {ret:.2%}")
        try:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='sell',
                type='market',
                time_in_force='day'
            )
            print(f"  Submitted sell order: {order.id}")
            stopped_symbols.append(symbol)
        except Exception as e:
            print(f"  Failed to submit order: {e}")

if stopped_symbols:
    print(f"Stopped symbols: {stopped_symbols}")
    # Try to send Telegram message via telegram_notifier.py
    try:
        msg = f"Stop fired for {', '.join(stopped_symbols)} at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        # Use os.system to call the notifier script
        os.system(f'python telegram_notifier.py "{msg}"')
        print("Attempted to send Telegram message.")
    except Exception as e:
        print(f"Could not send Telegram message: {e}")
else:
    print("No stops triggered.")
