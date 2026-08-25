import os
import json
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi

# Load environment variables
load_dotenv()

# Initialize Alpaca API
api = tradeapi.REST(
    os.getenv('ALPACA_API_KEY'),
    os.getenv('ALPACA_SECRET_KEY'),
    os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
)

# Get the last 2 days of SPY data
try:
    bars = api.get_bars('SPY', tradeapi.TimeFrame.Day, limit=2).df
    if len(bars) >= 2:
        close_yesterday = bars.iloc[-2]['close']
        close_today = bars.iloc[-1]['close']
        spy_change_pct = (close_today - close_yesterday) / close_yesterday * 100
    else:
        spy_change_pct = 0.0
except Exception as e:
    print(f"Error fetching SPY data: {e}")
    spy_change_pct = 0.0

print(f"SPY change: {spy_change_pct:+.2f}%")

# Path to the strategy file
strategy_path = '/Users/peter/Desktop/Old_Projects/Github/Alpaca_Paper_Trader/strategy_high_risk.json'

# Load the current strategy
with open(strategy_path, 'r') as f:
    data = json.load(f)

# Update the strategy with the SPY change
data['updated_by'] = f'spy_change_{spy_change_pct:+.2f}%'
data['last_updated'] = '2026-08-13'  # Today's date

# Write back the updated strategy
with open(strategy_path, 'w') as f:
    json.dump(data, f, indent=2)

print("Updated strategy_high_risk.json with SPY change.")
