#!/usr/bin/env python3
"""
Inverse ETF hedge manager for High Risk wallet.
Maintains a target allocation to an inverse ETF (e.g., SH) as a proxy for short exposure.
"""
import json
import os
import sys
import requests
from datetime import datetime, timezone

sys.path.insert(0, '/Users/peter/GitHub/Alpaca_Paper_Trader')
from capitol_copier import BASE_URL, ALPACA_HEADERS, get_positions, place_market_order, get_account_equity, DATA_URL

# Configuration
INVERSE_ETF = "SH"          # ProShares Short S&P500 (-1x)
# Hedge size follows the market instead of sitting flat. A permanent 5% short
# is a constant drag while the market rises, and only 5% of protection when it
# falls — slightly wrong in both directions. Regime comes from
# market_context.regime_state(), cached per session.
DEFAULT_HEDGE_BY_REGIME = {"bull": 0.00, "neutral": 0.05, "bear": 0.15}
TARGET_PCT = 0.05           # fallback when the regime cannot be read


def hedge_target_pct():
    """Share of equity to hold in the inverse ETF, given today's regime."""
    try:
        import json as _json
        cfg = _json.load(open("/Users/peter/GitHub/Alpaca_Paper_Trader/strategy_high_risk.json"))
        table = (cfg.get("risk") or {}).get("hedge_by_regime") or DEFAULT_HEDGE_BY_REGIME
    except Exception:
        table = DEFAULT_HEDGE_BY_REGIME
    try:
        import market_context
        state = market_context.regime_state().get("state", "unknown")
    except Exception:
        state = "unknown"
    if state == "unknown":
        return TARGET_PCT, state
    try:
        return max(0.0, min(1.0, float(table.get(state, TARGET_PCT)))), state
    except Exception:
        return TARGET_PCT, state
CHECK_INTERVAL_SECONDS = 1800  # 30 minutes (can be overridden by cron schedule)

def get_equity():
    equity = get_account_equity()
    return equity or 0.0

def get_position(symbol):
    positions = get_positions()
    for p in positions:
        if p['symbol'].upper() == symbol.upper():
            return p
    return None

def get_current_price(symbol):
    # Use latest trade price via data API
    # DATA_URL is already set to "https://data.alpaca.markets/v2" in capitol_copier
    url = f"{DATA_URL}/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        return data.get('trade', {}).get('p', 0.0)
    # fallback: use previous close from position if needed
    pos = get_position(symbol)
    if pos:
        market_val = float(pos.get('market_value', 0))
        qty = float(pos.get('qty', 0))
        if qty != 0:
            return market_val / qty
    return 0.0

def main():
    equity = get_equity()
    if equity <= 0:
        print("[inverse_hedge] Invalid equity, exiting")
        return
    pct, regime = hedge_target_pct()
    target_notional = equity * pct
    print(f"[inverse_hedge] Equity: ${equity:,.2f}, regime {regime}, "
          f"Target {INVERSE_ETF} notional: ${target_notional:,.2f} ({pct*100:.0f}%)")
    
    pos = get_position(INVERSE_ETF)
    cur_qty = float(pos.get('qty', 0)) if pos else 0.0
    cur_price = get_current_price(INVERSE_ETF)
    if cur_price <= 0:
        print(f"[inverse_hedge] Unable to get price for {INVERSE_ETF}, skipping")
        return
    cur_notional = abs(cur_qty * cur_price)
    print(f"[inverse_hedge] Current {INVERSE_ETF}: qty {cur_qty:.4f}, price ${cur_price:.2f}, notional ${cur_notional:,.2f}")
    
    # Determine action
    diff_notional = target_notional - cur_notional
    if abs(diff_notional) < 10:  # $10 threshold
        print(f"[inverse_hedge] Within threshold, no action needed")
        return
    
    if diff_notional > 0:
        # Need to buy more
        side = "buy"
        notional = diff_notional
    else:
        # Need to sell
        side = "sell"
        notional = abs(diff_notional)
        # Ensure we don't sell more than we have
        if notional > cur_notional:
            notional = cur_notional
            print(f"[inverse_hedge] Clipped sell notional to current holding")
    
    if notional <= 0:
        print(f"[inverse_hedge] No action after clipping")
        return
    
    print(f"[inverse_hedge] Placing {side} order for {INVERSE_ETF} notional ${notional:,.2f}")
    try:
        if side == "buy":
            res = place_market_order(INVERSE_ETF, "buy", notional=notional)
        else:
            pos = get_position(INVERSE_ETF)
            if pos:
                qty = abs(float(pos.get('qty', 0)))
                res = place_market_order(INVERSE_ETF, "sell", qty=qty)
            else:
                print(f"[inverse_hedge] No position to sell")
                return
        if res.get('id'):
            print(f"[inverse_hedge] Order placed: {res['id']}")
        else:
            print(f"[inverse_hedge] Order failed: {res}")
    except Exception as e:
        print(f"[inverse_hedge] Exception: {e}")

if __name__ == '__main__':
    main()
