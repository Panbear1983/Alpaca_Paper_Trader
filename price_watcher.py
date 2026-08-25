#!/usr/bin/env python3
"""
Price watcher that enforces:
- Sell entire position if price drops >=5% from average entry price (stop loss)
- Buy back in if price drops >=20% from the original entry price (after a stop-loss sell)
Runs independently of the main scheduler, checking prices every 30 seconds during market hours.
"""
import json
import os
import sys
import time
from datetime import datetime

# Add project root to path
sys.path.insert(0, '/Users/peter/GitHub/Alpaca_Paper_Trader')

from capitol_copier import (
    BASE_URL,
    ALPACA_HEADERS,
    get_positions,
    place_market_order,
    get_account_equity,
)
import requests
from utils.world_clock import WorldClock

# Constants
STATE_FILE = os.path.expanduser('~/.hermes/profiles/wanna_buffet/price_watcher_state.json')
# No symbol whitelist: the stop-loss applies to every open position. The
# photonic/semiconductor list that used to live here was revoked 2026-08-19.
STOP_LOSS_PCT = 0.05   # 5%
BUY_BACK_PCT = 0.20    # 20% drop from entry price to trigger re-entry
CHECK_INTERVAL = 30    # seconds between checks
BASE_POSITION_USD = 7500  # default; will be overridden from config

# Load ISR Alpha base position from strategy config
def load_base_position_usd():
    global BASE_POSITION_USD
    try:
        config_path = '/Users/peter/GitHub/Alpaca_Paper_Trader/strategy_high_risk.json'
        with open(config_path) as f:
            cfg = json.load(f)
        isr_cfg = cfg.get('isr_alpha', {})
        BASE_POSITION_USD = isr_cfg.get('base_position_usd', 7500)
        print(f"[watcher] Loaded base_position_usd: {BASE_POSITION_USD}")
    except Exception as e:
        print(f"[watcher] Could not load base position from config: {e}; using default {BASE_POSITION_USD}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[watcher] Failed to load state: {e}")
    return {}

def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[watcher] Failed to save state: {e}")

def is_market_open():
    wc = WorldClock()
    return wc.is_market_open()

def get_current_price(symbol):
    """Fetch latest trade price for symbol using Alpaca data API."""
    try:
        base_url = BASE_URL or ''
        if not base_url:
            return 0.0
        data_url = base_url.replace('https://api.alpaca.markets', 'https://data.alpaca.markets')
        url = f"{data_url}/v2/stocks/{symbol}/trades/latest"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('trade', {}).get('p', 0.0)
        else:
            # Fallback: use last quote or previous close? We'll use previous close from position's market_value/qty if needed.
            return 0.0
    except Exception:
        return 0.0

def main():
    load_base_position_usd()
    state = load_state()
    print(f"[watcher] Starting price watcher. Checking every {CHECK_INTERVAL}s during market hours.")
    print(f"[watcher] Stop loss: {STOP_LOSS_PCT*100}%, Buy-back trigger: {BUY_BACK_PCT*100}% drop from entry price.")
    print("[watcher] Scope: all open positions (no symbol whitelist).")
    
    while True:
        if not is_market_open():
            print(f"[watcher] Market closed. Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)
            continue
        
        try:
            equity = get_account_equity() or 0
            positions = get_positions()
            pos_map = {p['symbol'].upper(): p for p in positions}
            
            # Process existing positions for stop loss
            for symbol, pos in pos_map.items():
                qty = float(pos.get('qty', 0))
                if qty == 0:
                    continue
                avg_entry = float(pos.get('avg_entry_price', 0))
                if avg_entry == 0:
                    continue
                # Update state with current entry price
                state.setdefault(symbol, {})['entry_price'] = avg_entry
                
                # Get current price
                current_price = get_current_price(symbol)
                if current_price <= 0:
                    # Fallback to using market_value/qty if trade price unavailable
                    market_val = float(pos.get('market_value', 0))
                    if qty != 0:
                        current_price = market_val / qty
                if current_price == 0:
                    continue
                
                pnl_pct = (current_price - avg_entry) / avg_entry
                if pnl_pct <= -STOP_LOSS_PCT:
                    # Trigger stop loss: sell all
                    print(f"[watcher] STOP LOSS: {symbol} @ {current_price:.2f} (entry {avg_entry:.2f}) -> {pnl_pct*100:.2f}% -> SELL ALL {qty}")
                    try:
                        res = place_market_order(symbol, 'sell', qty=qty)
                        if res.get('id'):
                            print(f"[watcher]   Order placed: {res['id']}")
                            # After selling, keep entry_price in state for potential buy-back
                            state[symbol]['stop_loss_sold'] = True
                            state[symbol]['sold_price'] = current_price
                        else:
                            print(f"[watcher]   Order failed: {res}")
                    except Exception as e:
                        print(f"[watcher]   Exception placing order: {e}")
                else:
                    # Not below stop loss; ensure we have entry price recorded
                    if symbol not in state:
                        state[symbol] = {}
                    state[symbol]['entry_price'] = avg_entry
            
            # Process potential buy-back for tickers we don't hold but have an entry price from prior sale
            for symbol in list(state.keys()):
                if symbol in pos_map:
                    continue  # already have position
                entry_info = state.get(symbol, {})
                # Only re-enter names OUR stop-loss sold. Without this we would also
                # buy back anything another engine deliberately exited.
                if not entry_info.get('stop_loss_sold'):
                    continue
                entry_price = entry_info.get('entry_price')
                if entry_price is None:
                    continue
                # Check if we have already bought back recently? We'll just rely on price condition.
                current_price = get_current_price(symbol)
                if current_price <= 0:
                    continue
                # If price dropped >= BUY_BACK_PCT from entry price, consider buying
                if (current_price - entry_price) / entry_price <= -BUY_BACK_PCT:
                    # Determine order size: use base position size, but limit by available cash
                    cash = get_account_equity() or 0
                    # Use a fraction of equity? We'll just use base position size, but ensure we don't exceed cash
                    order_usd = min(BASE_POSITION_USD, cash * 0.5)  # use up to 50% of cash per buy
                    if order_usd < 100:  # minimum meaningful order
                        print(f"[watcher]   Insufficient cash for {symbol}: cash={cash:.2f}")
                        continue
                    qty = order_usd / current_price if current_price > 0 else 0
                    if qty <= 0:
                        continue
                    print(f"[watcher] BUY-BACK TRIGGER: {symbol} @ {current_price:.2f} (entry {entry_price:.2f}) -> {((current_price/entry_price)-1)*100:.2f}% -> BUY ${order_usd:.2f} ({qty:.4f} shares)")
                    try:
                        res = place_market_order(symbol, 'buy', notional=order_usd)
                        if res.get('id'):
                            print(f"[watcher]   Order placed: {res['id']}")
                            # After buying, update entry price to the price we bought at (approximate)
                            state[symbol]['entry_price'] = current_price
                            # Remove stop_loss_sold flag as we now have a position again
                            state[symbol].pop('stop_loss_sold', None)
                        else:
                            print(f"[watcher]   Order failed: {res}")
                    except Exception as e:
                        print(f"[watcher]   Exception placing order: {e}")
            
            # Save state after each loop
            save_state(state)
            
        except Exception as e:
            print(f"[watcher] Unexpected error in main loop: {e}")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[watcher] Stopped by user.")
        sys.exit(0)