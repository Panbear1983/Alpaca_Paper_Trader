#!/usr/bin/env python3
"""
Opportunistic stock picker for High Risk wallet.
Scans broader US market for high-momentum, high-volume stocks outside ISR universe.
"""
import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/Users/peter/GitHub/Alpaca_Paper_Trader')
from capitol_copier import BASE_URL, ALPACA_HEADERS, get_positions, place_market_order, get_account_equity

# Configuration
MAX_POSITIONS = 5           # Max concurrent opportunistic positions
POSITION_SIZE_PCT = 0.02    # 2% of equity per position
MIN_PRICE = 5.0             # Minimum stock price
MIN_VOLUME = 1000000        # Minimum average daily volume
LOOKBACK_DAYS = 20          # For volume average
MOMENTUM_LOOKBACK = 5       # Days for momentum calculation
REBALANCE_HOURS = 4         # How often to recheck (hours)

def get_equity():
    equity = get_account_equity()
    return equity or 0.0

def get_all_assets():
    """Get all active US equities from Alpaca"""
    url = f"{BASE_URL}/v2/assets"
    params = {
        'status': 'active',
        'asset_class': 'us_equity'
    }
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"[opportunistic] Failed to get assets: {resp.status_code}")
        return []
    assets = resp.json()
    # Filter for NYSE and NASDAQ (main exchanges)
    exchange_filter = ['NYSE', 'NASDAQ', 'ARCA']
    symbols = [a['symbol'] for a in assets if a['exchange'] in exchange_filter and a['tradable']]
    return symbols

def get_bar_data(symbol, timeframe='1Day', limit=30):
    """Get historical bar data for a symbol"""
    data_url = BASE_URL.replace('https://api.alpaca.markets', 'https://data.alpaca.markets')
    url = f"{data_url}/v2/stocks/{symbol}/bars"
    params = {
        'timeframe': timeframe,
        'limit': limit,
        'adjustment': 'raw'
    }
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=10)
    if resp.status_code != 200:
        return None
    return resp.json()

def calculate_momentum_and_volume(symbol):
    """Calculate momentum score and average volume"""
    bars = get_bar_data(symbol, limit=max(LOOKBACK_DAYS, MOMENTUM_LOOKBACK)+5)
    if not bars or len(bars) < 2:
        return 0, 0
    
    # Calculate average volume
    volumes = [bar['v'] for bar in bars if 'v' in bar]
    if len(volumes) < LOOKBACK_DAYS:
        avg_volume = 0
    else:
        avg_volume = sum(volumes[-LOOKBACK_DAYS:]) / LOOKBACK_DAYS
    
    # Calculate momentum (price change over MOMENTUM_LOOKBACK days)
    if len(bars) < MOMENTUM_LOOKBACK + 1:
        momentum = 0
    else:
        old_price = bars[-(MOMENTUM_LOOKBACK+1)]['c']
        new_price = bars[-1]['c']
        if old_price > 0:
            momentum = (new_price - old_price) / old_price
        else:
            momentum = 0
    
    return momentum, avg_volume

def get_current_price(symbol):
    """Get latest trade price"""
    data_url = BASE_URL.replace('https://api.alpaca.markets', 'https://data.alpaca.markets')
    url = f"{data_url}/v2/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        return data.get('trade', {}).get('p', 0.0)
    return 0.0

def main():
    print(f"[opportunistic] Starting scan at {datetime.now(timezone.utc).isoformat()}")
    
    equity = get_equity()
    if equity <= 0:
        print("[opportunistic] Invalid equity, exiting")
        return
    
    print(f"[opportunistic] Equity: ${equity:,.2f}")
    
    # Get current positions to avoid duplicates
    positions = get_positions()
    held_symbols = {p['symbol'].upper() for p in positions}
    print(f"[opportunistic] Currently holding: {len(held_symbols)} positions")
    
    # Get all tradable US equities
    all_symbols = get_all_assets()
    print(f"[opportunistic] Scanning {len(all_symbols)} symbols from NYSE/NASDAQ/ARCA")
    
    # Score each symbol
    scored_symbols = []
    for symbol in all_symbols[:200]:  # Limit to first 200 for speed (can increase)
        if symbol in held_symbols:
            continue
            
        price = get_current_price(symbol)
        if price < MIN_PRICE:
            continue
            
        momentum, avg_volume = calculate_momentum_and_volume(symbol)
        if avg_volume < MIN_VOLUME:
            continue
            
        # Combined score: momentum + volume factor (normalize volume)
        volume_score = min(avg_volume / 5000000, 2.0)  # Cap at 2x for 5M+ volume
        score = momentum * 0.7 + (volume_score - 1) * 0.3  # Weight momentum higher
        
        if score > 0:  # Only positive momentum
            scored_symbols.append((symbol, score, price, momentum, avg_volume))
    
    # Sort by score descending
    scored_symbols.sort(key=lambda x: x[1], reverse=True)
    
    print(f"[opportunistic] Found {len(scored_symbols)} candidates after filtering")
    if scored_symbols:
        print("[opportunistic] Top 5 candidates:")
        for sym, score, price, mom, vol in scored_symbols[:5]:
            print(f"  {sym}: score={score:.3f}, mom={mom*100:.1f}%, vol={vol:,.0f}, price=${price:.2f}")
    
    # Determine how many new positions we can open
    current_count = len([p for p in positions if p['symbol'].upper() not in ['SH']])  # Exclude hedge
    available_slots = max(0, MAX_POSITIONS - current_count)
    
    if available_slots <= 0:
        print(f"[opportunistic] No available slots (holding {current_count} positions, max {MAX_POSITIONS})")
        return
    
    # Take top candidates
    to_buy = scored_symbols[:available_slots]
    
    for symbol, score, price, momentum, avg_volume in to_buy:
        notional = equity * POSITION_SIZE_PCT
        if notional < 100:  # Minimum meaningful order
            continue
            
        print(f"[opportunistic] BUY {symbol}: ${notional:,.2f} ({notional/price:.4f} shares) "
              f"score={score:.3f}, mom={momentum*100:.1f}%")
        
        try:
            res = place_market_order(symbol, 'buy', notional=notional)
            if res.get('id'):
                print(f"[opportunistic]   Order placed: {res['id']}")
            else:
                print(f"[opportunistic]   Order failed: {res}")
        except Exception as e:
            print(f"[opportunistic]   Exception: {e}")
    
    print(f"[opportunistic] Scan complete at {datetime.now(timezone.utc).isoformat()}")

if __name__ == '__main__':
    main()