import alpaca_trade_api as tradeapi
import os
import sys

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_SECRET_KEY')
BASE_URL = os.getenv('ALPACA_BASE_URL')

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

def get_equity():
    account = api.get_account()
    return float(account.equity)

def main():
    equity = get_equity()
    print(f"Equity: ${equity:.2f}")
    
    # Risk per trade: 2% of equity
    risk_pct = 0.02
    risk_amount = equity * risk_pct
    print(f"Risk amount per trade: ${risk_amount:.2f}")
    
    # Opportunistic picks from the latest run (we can also fetch them again, but we'll use the ones we saw)
    # We'll use the top 3 by score: ZS, ZM, ZETA
    picks = [
        ('ZS', 187.25),
        ('ZM', 100.29),
        ('ZETA', 30.23)
    ]
    
    for symbol, price in picks:
        # Calculate quantity
        qty = risk_amount / price
        # Limit price: 1% above current pre-market price, rounded to 2 decimal places
        limit_price = round(price * 1.01, 2)
        try:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='buy',
                type='limit',
                time_in_force='day',
                limit_price=limit_price
            )
            print(f"Submitted LIMIT order for {symbol}: {qty:.4f} shares @ ${limit_price:.2f}")
        except Exception as e:
            print(f"Failed to submit order for {symbol}: {e}")

if __name__ == '__main__':
    main()
