import os
from alpaca_trade_api.rest import REST
api_key = os.getenv('ALPACA_API_KEY')
api_secret = os.getenv('ALPACA_SECRET_KEY')
base_url = os.getenv('ALPACA_BASE_URL')
client = REST(api_key, api_secret, base_url, api_version='v2')
positions = client.list_positions()
for p in positions:
    symbol = p.symbol
    avg_entry = float(p.avg_entry_price)
    current = float(p.current_price)  # need to check if attribute exists
    # Actually we can use last trade price? Let's see what fields are available.
    # We'll compute unrealized_inplc maybe? Better to get latest quote.
    # For simplicity, we can use p.market_value and p.qty to infer current price if qty !=0
    if float(p.qty) != 0:
        current_price = float(p.market_value) / float(p.qty)
    else:
        current_price = 0.0
    change_pct = (current_price - avg_entry) / avg_entry
    print(f'{symbol}: entry ${avg_entry:.2f}, current ${current_price:.2f}, change {change_pct*100:.2f}%')
    if change_pct <= -0.05:
        print(f'  -> STOP LOSS TRIGGERED (<= -5%)')
