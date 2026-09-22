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
    DATA_URL,
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
        # 2026-08-30: this used to derive the data host from BASE_URL with a
        # replace() that never matched the paper host, so every call 404'd and
        # the fallback (market_value / qty) — a stale price — was used instead.
        url = f"{DATA_URL}/stocks/{symbol}/trades/latest"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('trade', {}).get('p', 0.0)
        else:
            # Fallback: use last quote or previous close? We'll use previous close from position's market_value/qty if needed.
            return 0.0
    except Exception:
        return 0.0

# ── Heartbeat (read by event_watcher.check_heartbeats) ────────────────────────

HEARTBEAT_FILE = '/Users/peter/GitHub/Alpaca_Paper_Trader/diary/watcher_heartbeat.json'


def _write_heartbeat(n_positions: int, n_priced: int) -> None:
    """Atomic; never raises. Only called after a full, successful sweep."""
    try:
        import tempfile
        from zoneinfo import ZoneInfo
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        payload = {"epoch": time.time(),
                   "ts": datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
                   "n_positions": int(n_positions), "n_priced": int(n_priced),
                   "pid": os.getpid()}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(HEARTBEAT_FILE), prefix=".hb.", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        print(f"[watcher] heartbeat not written: {e}")


# ── Broker-side guards (broker_stops.py) ──────────────────────────────────────

_GUARD_LAST = {"t": 0.0}


def _reconcile_guards(every_s: float = 0.0) -> None:
    """Call broker_stops.reconcile(); swallow everything. `every_s` adds a
    coarser interval for the market-closed branch."""
    if every_s and time.time() - _GUARD_LAST["t"] < every_s:
        return
    _GUARD_LAST["t"] = time.time()
    try:
        import broker_stops
        for row in broker_stops.reconcile():
            print(f"[stops] {row.get('sym')} {row.get('action')} {row.get('reason','')} "
                  f"{row.get('result') or row.get('error') or ''}")
    except Exception as e:
        print(f"[stops] reconcile failed: {e}")


def _stop_frac(symbol: str) -> float:
    """Stop distance as a fraction: the per-name guard width on a boxed
    wallet, STOP_LOSS_PCT (5%) everywhere else."""
    try:
        import broker_stops
        if broker_stops.enabled():
            return broker_stops.width_pct(symbol) / 100.0
    except Exception:
        pass
    return STOP_LOSS_PCT


# ── Buy-back mode (price_watcher block of strategy_high_risk.json) ────────────

_WATCHER_CFG = {"t": 0.0, "cfg": {}}


def _watcher_cfg():
    """price_watcher block of strategy_high_risk.json, re-read at most once a
    minute — this process never restarts, so a config edit must be seen live."""
    now = time.time()
    if now - _WATCHER_CFG["t"] > 60:
        try:
            with open('/Users/peter/GitHub/Alpaca_Paper_Trader/strategy_high_risk.json') as f:
                _WATCHER_CFG["cfg"] = (json.load(f).get("price_watcher") or {})
        except Exception as e:
            print(f"[watcher] could not read config: {e}")
        _WATCHER_CFG["t"] = now
    return _WATCHER_CFG["cfg"]


def buyback_decision(mode: str, notified_date: str | None, today: str) -> str | None:
    """What to do when a stopped-out name is 20% below its old entry.

    'trade'  → the original automatic buy (still through every gate).
    'notify' → a Telegram line, once per symbol per day, no order.  (default)
    'off'    → nothing.
    Peter trades by hand now (2026-09-22): a machine buying a falling stock
    without asking was the wrong default for that.
    """
    mode = (mode or "notify").lower()
    if mode == "trade":
        return "trade"
    if mode == "notify":
        return None if notified_date == today else "notify"
    return None


# ── Anchor plan manager ───────────────────────────────────────────────────────

_ANCHOR_CFG = {"t": 0.0, "cfg": {}}


def _anchor_cfg():
    """anchor block of strategy_high_risk.json, re-read at most once a minute."""
    now = time.time()
    if now - _ANCHOR_CFG["t"] > 60:
        try:
            with open('/Users/peter/GitHub/Alpaca_Paper_Trader/strategy_high_risk.json') as f:
                _ANCHOR_CFG["cfg"] = (json.load(f).get("anchor") or {})
        except Exception as e:
            print(f"[anchor] could not read config: {e}")
        _ANCHOR_CFG["t"] = now
    return _ANCHOR_CFG["cfg"]


def _now_iso():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")


def _order_status(order_id):
    try:
        r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=ALPACA_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _sell_fills_since(symbol, since_iso):
    """Filled sell orders for symbol submitted after since_iso: [(qty, price), ...]."""
    try:
        r = requests.get(f"{BASE_URL}/orders", headers=ALPACA_HEADERS, timeout=15,
                         params={"status": "closed", "after": since_iso, "limit": 200,
                                 "direction": "asc", "symbols": symbol})
        out = []
        for o in (r.json() if r.status_code == 200 else []):
            if o.get("side") == "sell" and o.get("status") == "filled":
                out.append((float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)))
        return out
    except Exception:
        return []


def _recent_5min_lows(symbol, n):
    """Lows of the last n COMPLETED 5-minute bars of the current session (IEX feed)."""
    from datetime import timedelta, timezone as _tz
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now = datetime.now(_tz.utc)
    start = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0).astimezone(_tz.utc)
    try:
        r = requests.get(f"{DATA_URL}/stocks/{symbol}/bars", headers=ALPACA_HEADERS, timeout=15,
                         params={"timeframe": "5Min", "start": start.isoformat(),
                                 "feed": "iex", "limit": 200})
        bars = (r.json().get("bars") or []) if r.status_code == 200 else []
    except Exception:
        bars = []
    done = []
    for b in bars:
        t = datetime.fromisoformat(str(b.get("t", "")).replace("Z", "+00:00"))
        if t + timedelta(minutes=5) <= now:
            done.append(float(b.get("l") or 0))
    return [x for x in done[-n:] if x > 0]


def anchor_manage(pos_map, state):
    import anchor_journal as aj
    cfg = _anchor_cfg()
    rows = aj.read()
    open_rows = [r for r in rows if r.get("status") == "open"]
    n_open = len(open_rows)
    if state.get("_anchor_open_n") != n_open:
        print(f"[anchor] journal: {n_open} open")
        state["_anchor_open_n"] = n_open
    if not open_rows:
        return

    be_r     = float(cfg.get("breakeven_at_r") or 1.0)
    pt_r     = float(cfg.get("partial_take_at_r") or 2.0)
    pt_frac  = float(cfg.get("partial_take_frac") or 0.5)
    trail_n  = int(cfg.get("trail_bars") or 3)
    max_stop = float(cfg.get("max_stop_pct") or 0.05)
    now_ts   = time.time()
    changes  = {}          # row id -> dict of field updates

    for r in open_rows:
        rid, sym = r.get("id"), str(r.get("symbol", "")).upper()
        upd = {}
        pos = pos_map.get(sym)
        age = now_ts - datetime.fromisoformat(r["ts"]).timestamp() if r.get("ts") else 9e9

        if pos is None:
            if age < 120:
                continue                       # the buy may still be filling
            fills = _sell_fills_since(sym, r.get("ts", ""))
            if fills:
                q = sum(f[0] for f in fills) or 1.0
                exit_px = sum(f[0] * f[1] for f in fills) / q
                entry = float(r.get("entry") or r.get("est_entry") or exit_px)
                qty = float(r.get("qty") or (float(r.get("notional") or 0) / entry if entry else 0))
                pnl = round((exit_px - entry) * qty, 2)
                rr = float(r.get("r") or 0)
                upd.update({"status": "closed", "exit_price": round(exit_px, 4), "pnl": pnl,
                            "r_multiple": round((exit_px - entry) / rr, 2) if rr else None,
                            "reason": r.get("reason") or "closed_externally", "closed_at": _now_iso()})
                print(f"[anchor] {sym} closed externally at {exit_px:.2f}, pnl {pnl:+.0f}")
            else:
                o = _order_status(rid) if rid else {}
                if o and o.get("status") not in ("filled", "partially_filled", None):
                    upd.update({"status": "failed", "reason": f"buy order {o.get('status')}",
                                "closed_at": _now_iso()})
                    print(f"[anchor] {sym} buy order {o.get('status')} — row marked failed")
            if upd:
                changes[rid] = upd
            continue

        # first sight of the live position: lock in the real entry and size
        entry = float(r.get("entry") or 0)
        if not entry:
            entry = float(pos.get("avg_entry_price") or 0)
            if entry <= 0:
                continue
            stop = float(r.get("stop") or 0)
            if stop >= entry or stop < entry * (1 - max_stop):
                stop = round(entry * (1 - max_stop), 2)
                upd["stop"] = stop
            upd.update({"entry": entry, "qty": float(pos.get("qty") or 0),
                        "r": round(entry - stop, 4)})
            r = {**r, **upd}
            print(f"[anchor] {sym} filled at {entry:.2f}, stop {float(r['stop']):.2f}, R {float(r['r']):.2f}")

        stop = float(r.get("stop") or 0)
        rr = float(r.get("r") or 0)
        cur = get_current_price(sym) or (abs(float(pos.get("market_value") or 0)) / abs(float(pos.get("qty") or 1)))
        if cur <= 0 or rr <= 0:
            if upd:
                changes[rid] = upd
            continue
        qty_avail = abs(float(pos.get("qty_available", pos.get("qty", 0)) or 0))

        # 1. stop
        if cur <= stop and qty_avail > 0:
            kind = r.get("stop_kind", "initial")
            print(f"[anchor] STOP ({kind}) {sym} @ {cur:.2f} <= {stop:.2f} → sell all {qty_avail:g}")
            res = place_market_order(sym, "sell", qty=qty_avail)
            if res.get("id"):
                qty = float(r.get("qty") or qty_avail)
                upd.update({"status": "closed", "exit_price": cur, "pnl": round((cur - entry) * qty, 2),
                            "r_multiple": round((cur - entry) / rr, 2), "reason": f"stop_{kind}",
                            "closed_at": _now_iso()})
            else:
                print(f"[anchor]   sell failed: {res}")
            changes[rid] = upd
            continue

        # 2. break-even
        if not r.get("breakeven") and cur >= entry + be_r * rr:
            upd.update({"stop": round(entry, 2), "stop_kind": "breakeven", "breakeven": True})
            stop = entry
            print(f"[anchor] {sym} reached +{be_r:g}R ({cur:.2f}) → stop moved to break-even {entry:.2f}")

        # 3. partial
        if not r.get("partial_done") and cur >= entry + pt_r * rr and qty_avail > 0:
            q = round(qty_avail * pt_frac, 4)
            if q > 0:
                print(f"[anchor] {sym} reached +{pt_r:g}R ({cur:.2f}) → sell {pt_frac*100:.0f}% ({q:g})")
                res = place_market_order(sym, "sell", qty=q)
                if res.get("id"):
                    upd.update({"partial_done": True, "partial_at": cur, "partial_qty": q})

        # 4. trail, once past break-even, on the last completed 5-minute lows, every 5 min
        if (r.get("breakeven") or upd.get("breakeven")) and \
                now_ts - float(state.get(f"_anchor_trail_{rid}", 0)) >= 300:
            state[f"_anchor_trail_{rid}"] = now_ts
            lows = _recent_5min_lows(sym, trail_n)
            if lows:
                new_stop = round(min(lows), 2)
                if new_stop > stop and new_stop < cur:
                    upd.update({"stop": new_stop, "stop_kind": "trail"})
                    print(f"[anchor] {sym} trail: stop {stop:.2f} → {new_stop:.2f} (low of last {trail_n} bars)")

        if upd:
            changes[rid] = upd

    if changes:
        def _apply(rows_):
            hit = False
            for row in rows_:
                if row.get("id") in changes:
                    row.update(changes[row["id"]])
                    hit = True
            return hit
        aj.update(_apply)


def main():
    load_base_position_usd()
    state = load_state()
    print(f"[watcher] Starting price watcher. Checking every {CHECK_INTERVAL}s during market hours.")
    print(f"[watcher] Stop loss: {STOP_LOSS_PCT*100}%, Buy-back trigger: {BUY_BACK_PCT*100}% drop from entry price.")
    print("[watcher] Scope: all open positions (no symbol whitelist).")
    
    while True:
        if not is_market_open():
            print(f"[watcher] Market closed. Sleeping {CHECK_INTERVAL}s...")
            # GTC guards can be armed while the market is shut, so a position
            # opened late in a session is protected before the next open.
            _reconcile_guards(every_s=600)
            time.sleep(CHECK_INTERVAL)
            continue
        
        try:
            equity = get_account_equity() or 0
            positions = get_positions()
            pos_map = {p['symbol'].upper(): p for p in positions}
            n_priced = 0          # how many names we could actually price this sweep

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
                n_priced += 1

                pnl_pct = (current_price - avg_entry) / avg_entry
                # Boxed wallet: same per-name width as the broker guard, which
                # normally fires first; this is the backstop. Elsewhere: 5%.
                stop_frac = _stop_frac(symbol)
                if pnl_pct <= -stop_frac:
                    # Trigger stop loss: sell all
                    print(f"[watcher] STOP LOSS: {symbol} @ {current_price:.2f} (entry {avg_entry:.2f}) -> {pnl_pct*100:.2f}% <= -{stop_frac*100:.1f}% -> SELL ALL {qty}")
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
                # Bookkeeping keys ("_anchor_open_n" etc.) share this dict. One
                # of them is an int; .get() on it crashed every loop for weeks
                # (8,356 log lines) and nothing below this line ever ran.
                if symbol.startswith("_") or not isinstance(entry_info, dict):
                    continue
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
                    today_et = _now_iso()[:10]
                    verdict = buyback_decision(_watcher_cfg().get("buyback_mode", "notify"),
                                               entry_info.get("buyback_notified_date"), today_et)
                    if verdict is None:
                        continue
                    if verdict == "notify":
                        drop = (current_price / entry_price - 1) * 100
                        print(f"[watcher] BUY-BACK CANDIDATE (notify only): {symbol} @ {current_price:.2f}, "
                              f"{drop:.1f}% below the old entry {entry_price:.2f}")
                        try:
                            import telegram_notifier as tn
                            tn.notify_position_alert(
                                symbol, f"now {drop:.0f}% below your old entry ({entry_price:.2f} → "
                                        f"{current_price:.2f}). Buy-back candidate — nothing was bought.",
                                "info")
                            state[symbol]["buyback_notified_date"] = today_et
                        except Exception as e:
                            print(f"[watcher]   notify failed: {e}")
                        continue
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
            
            # ── Anchor plan: journal-driven stop management ───────────────
            # Rows in diary/anchor_trades.jsonl (written by anchor_trade.py) get a
            # structure stop, break-even at +1R, a partial at +2R and a trail under
            # the last completed 5-minute lows. The 5%-from-entry sweep above stays
            # as the floor under all of it.
            try:
                anchor_manage(pos_map, state)
            except Exception as e:
                print(f"[anchor] error: {e}")

            # Broker-side guards: make the resting trailing stops match the
            # book (boxed wallets only; rate-limits itself; never raises).
            _reconcile_guards()

            # Heartbeat — written ONLY here, after a complete sweep. launchd
            # restarts this process whenever it dies, so "is it running" means
            # nothing; "did it just finish pricing everything" is the signal
            # event_watcher's watchdog reads.
            _write_heartbeat(len(pos_map), n_priced)

        except Exception as e:
            print(f"[watcher] Unexpected error in main loop: {e}")
        finally:
            # Save state after each loop — in a finally so an exception above
            # can never skip it again.
            try:
                save_state(state)
            except Exception as e:
                print(f"[watcher] could not save state: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[watcher] Stopped by user.")
        sys.exit(0)