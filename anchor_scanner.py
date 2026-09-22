#!/usr/bin/env python3
"""
anchor_scanner.py — reads the charts for the anchor names and says whether a long setup exists.
Places no orders. Wanna Buffet reads this and decides; anchor_trade.py places the order.

  python3 anchor_scanner.py                 # silent unless in the entry window AND something is at least IN_ZONE
  python3 anchor_scanner.py --verbose       # always print the full table
  python3 anchor_scanner.py --session 2026-08-28 --at 10:30 --verbose   # replay a past session

Silence is deliberate: under Hermes cron, empty stdout means the agent is not woken at all, so
a quiet morning costs no model call.

The method, translated from Chris Kmer's write-up (WB's 2026-08-30 draft) into things Alpaca
bars can actually measure. Anything the draft asked for that the data cannot give — VIX, a
volume-profile point of control, footprint delta — is left out rather than guessed.

  ENVIRONMENT  SPY regime from market_context (bull / neutral / bear). Longs only, and only
               when the regime is not bear.
  LOCATION     On 60-minute bars over the last `swing_lookback_sessions` sessions: the swing
               high, and the swing low that preceded it. Fibonacci retracement from that high
               back toward the low: 61.8% and 78.6% bound the entry zone, 88.6% invalidates.
  CONFIRMATION On today's completed 5-minute bars, in order:
                 absorption  — a bar that prints the lowest low so far inside the zone, closes
                               in its upper half, on volume >= 1.5x the trailing 20-bar average
                               (sellers pushed hard and failed to hold the low)
                 dominance   — a green bar closing in its top quarter on volume >= 1.2x average
                               (buyers lifting the offer)
               CONFIRMED needs the sequence absorption -> dominance -> absorption -> dominance:
               sellers must try and fail TWICE before the entry. A close below the 88.6 level at
               any point marks the setup INVALIDATED for the session.
  STOP/TARGET  stop = low of the last absorption bar, never more than anchor.max_stop_pct below
               the current price; target = the swing high; R = price - stop.

Volumes are from the IEX feed, a fraction of consolidated tape. The volume tests are all
RELATIVE (to that same feed's own average) so they still mean something; the absolute
`min_confirm_volume` figure is reported as thin/ok and does not change the state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
os.chdir(_HERE)

import requests  # noqa: E402

import capitol_copier as cc  # noqa: E402
import entry_gate as eg  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
FIB = (0.618, 0.786, 0.886)


# ── data ────────────────────────────────────────────────────────────────────

def fetch_bars(symbols, timeframe, start, end):
    """Multi-symbol bars, iex feed, paginated. {sym: [bar,...]} oldest->newest.
    Copied from swing_buyer.fetch_daily_bars with timeframe/start/end parameters."""
    out, token = {}, None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe,
                  "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                  "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                  "adjustment": "split", "feed": "iex", "limit": 10000}
        if token:
            params["page_token"] = token
        r = requests.get(f"{cc.DATA_URL}/stocks/bars", headers=cc.ALPACA_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
    return out


def _t(bar) -> dt.datetime:
    return dt.datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(ET)


def _rth(bar) -> bool:
    t = _t(bar).time()
    return dt.time(9, 30) <= t < dt.time(16, 0)


# ── analysis ────────────────────────────────────────────────────────────────

def swing_levels(h_bars):
    """(swing_low, swing_high) on 60-minute bars: the highest high in the window and the
    lowest low BEFORE it. None if there is no low-to-high leg."""
    bars = [b for b in h_bars if _rth(b)]
    if len(bars) < 8:
        return None
    hi_i = max(range(len(bars)), key=lambda i: float(bars[i]["h"]))
    if hi_i == 0:
        return None
    lo_i = min(range(hi_i), key=lambda i: float(bars[i]["l"]))
    lo, hi = float(bars[lo_i]["l"]), float(bars[hi_i]["h"])
    if hi <= lo or (hi - lo) / hi < 0.015:          # under 1.5% is noise, not a swing
        return None
    return lo, hi, _t(bars[lo_i]), _t(bars[hi_i])


def analyse(sym, h_bars, m5_bars, cfg, now):
    res = {"symbol": sym, "state": "NO_SETUP", "why": "", "price": None}
    done = [b for b in m5_bars if _rth(b) and _t(b) + dt.timedelta(minutes=5) <= now]
    if done:
        res["price"] = float(done[-1]["c"])
    sw = swing_levels(h_bars)
    if not sw:
        res["why"] = "no clear low-to-high swing on the 60-minute chart"
        return res
    lo, hi, lo_t, hi_t = sw
    rng = hi - lo
    f618, f786, f886 = (round(hi - x * rng, 2) for x in FIB)
    res.update({"swing_low": lo, "swing_high": hi, "swing_low_at": lo_t.strftime("%m-%d %H:%M"),
                "swing_high_at": hi_t.strftime("%m-%d %H:%M"), "f618": f618, "f786": f786, "f886": f886})
    if not done:
        res["why"] = "no completed 5-minute bars yet"
        return res

    price = res["price"]
    # position relative to the zone right now
    if price > f618:
        loc = "above zone (premium)"
    elif price >= f786:
        loc = "IN ZONE"
    elif price >= f886:
        loc = "deep pullback (between 78.6 and 88.6)"
    else:
        loc = "below 88.6"
    res["location"] = loc

    stage = 0              # 0 none, 1 absorption#1, 2 dominance#1, 3 absorption#2, 4 dominance#2 = confirmed
    zone_low = None
    last_abs = None
    invalid = False
    touched = False
    vols = []
    for b in done:
        o, h, l, c, v = (float(b[k]) for k in ("o", "h", "l", "c", "v"))
        avg = sum(vols[-20:]) / len(vols[-20:]) if vols else 0.0
        vols.append(v)
        span = max(h - l, 1e-9)
        if c < f886:
            invalid = True
            break
        in_zone = l <= f618 and c >= f886
        if not in_zone:
            continue
        touched = True
        new_low = zone_low is None or l <= zone_low
        zone_low = l if zone_low is None else min(zone_low, l)
        absorption = new_low and (c - l) >= 0.5 * span and avg > 0 and v >= 1.5 * avg
        dominance = c > o and (c - l) >= 0.75 * span and avg > 0 and v >= 1.2 * avg
        if stage in (0, 2) and absorption:
            stage += 1
            last_abs = b
        elif stage in (1, 3) and dominance:
            stage += 1
            res["confirm_bar"] = b
            if stage == 4:
                break

    if invalid:
        res["state"] = "INVALIDATED"
        res["why"] = f"a 5-minute close fell below the 88.6 level ({f886})"
        return res
    if not touched:
        res["state"] = "NO_SETUP"
        res["why"] = f"price has not pulled back into the zone {f786}–{f618} today ({loc})"
        return res
    labels = {0: "IN_ZONE", 1: "ABSORPTION", 2: "ABSORPTION", 3: "ABSORPTION", 4: "CONFIRMED"}
    res["state"] = labels[stage]
    res["stage"] = stage
    res["why"] = {0: "in the zone, no absorption bar yet",
                  1: "first absorption seen, waiting for buyers",
                  2: "first seller failure done, waiting for the second test",
                  3: "second absorption seen, waiting for the buyers' bar",
                  4: "sellers failed twice and buyers took over"}[stage]
    max_stop = float(cfg.get("max_stop_pct") or 0.05)
    if last_abs is not None:
        stop = float(last_abs["l"])
    else:
        stop = zone_low if zone_low else f886
    stop = round(max(stop, price * (1 - max_stop)), 2)
    r = price - stop
    res.update({"stop": stop, "target": round(hi, 2), "r": round(r, 2),
                "rr": round((hi - price) / r, 2) if r > 0 else None})
    cb = res.get("confirm_bar")
    if cb:
        v = float(cb["v"])
        res["confirm_vol"] = int(v)
        res["vol_ok"] = v >= float(cfg.get("min_confirm_volume") or 0)
    return res


# ── output ──────────────────────────────────────────────────────────────────

def render(now, st, regime, rows, cfg):
    L = []
    L.append(f"ANCHOR SCAN  {now:%a %Y-%m-%d %H:%M} ET   entry window {st['window'][0]}-{st['window'][1]}: "
             f"{'OPEN' if st['in_window'] else 'closed'}   regime: {regime}")
    chg = st.get("day_change_pct")
    flags = []
    if st.get("daily_loss_halt"):
        flags.append("DAILY LOSS HALT — no new entries")
    if st.get("loss_halt"):
        flags.append("LOSS-STREAK HALT — no new entries")
    if regime == "bear":
        flags.append("BEAR REGIME — no longs")
    if st.get("market_open") is False:
        flags.append("MARKET CLOSED")
    L.append(f"day P&L {chg:+.2f}%" if chg is not None else "day P&L unknown")
    used = st.get("entries_today") or {}
    L.append("entries used today: " + (", ".join(f"{k} {v}/{cfg.get('max_entries_per_name_per_day', 2)}"
                                                 for k, v in sorted(used.items())) or "none"))
    for f in flags:
        L.append(f"!! {f}")
    L.append("")
    def _f(v, fmt="{:.2f}"):
        return fmt.format(v) if v not in (None, 0, "") else "-"

    L.append(f"{'SYM':5s} {'PRICE':>8s} {'STATE':12s} {'ZONE(78.6-61.8)':>17s} {'88.6':>8s} "
             f"{'STOP':>8s} {'TARGET':>8s} {'R':>6s} {'R:R':>5s}  NOTE")
    for r in rows:
        zone = f"{r['f786']:.2f}-{r['f618']:.2f}" if r.get("f618") else "-"
        note = r.get("why", "")
        if r.get("swing_low"):
            note += (f" (swing {r['swing_low']:.2f}@{r['swing_low_at']} -> "
                     f"{r['swing_high']:.2f}@{r['swing_high_at']})")
        if r.get("confirm_vol"):
            note += f" vol {r['confirm_vol']:,} {'ok' if r.get('vol_ok') else 'thin'}"
        rr = r.get("rr")
        L.append(f"{r['symbol']:5s} {_f(r.get('price')):>8s} {r['state']:12s} {zone:>17s} "
                 f"{_f(r.get('f886')):>8s} {_f(r.get('stop')):>8s} {_f(r.get('target')):>8s} "
                 f"{_f(r.get('r')):>6s} {(_f(rr, '{:.1f}') if rr is not None else '-'):>5s}  {note}")
    L.append("")
    blocked = bool(flags) or not st["in_window"]
    cands = [r for r in rows if r["state"] == "CONFIRMED" and (r.get("rr") or 0) >= 1.5
             and used.get(r["symbol"], 0) < int(cfg.get("max_entries_per_name_per_day") or 2)]
    if cands and not blocked:
        L.append("ACTION: candidates " + "; ".join(
            f"{r['symbol']} stop={r['stop']:.2f} target={r['target']:.2f} rr={r['rr']:.1f}" for r in cands))
        L.append("        place with: python3 anchor_trade.py buy SYM --stop STOP --note '<why>'  (one name per run)")
    elif cands and blocked:
        L.append("ACTION: none — " + "; ".join(f"{r['symbol']} is confirmed" for r in cands)
                 + " but " + (flags[0] if flags else "the entry window is closed"))
    else:
        L.append("ACTION: none")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Anchor-name setup scanner (no orders).")
    ap.add_argument("--verbose", action="store_true", help="always print, even when nothing is in play")
    ap.add_argument("--session", help="replay this session date (YYYY-MM-DD)")
    ap.add_argument("--at", default="11:30", help="ET time within --session (HH:MM), default 11:30")
    a = ap.parse_args()

    cfg = eg.load_cfg()
    universe = [str(u).upper() for u in (cfg.get("universe") or [])]
    if not universe:
        print("no anchor universe configured")
        return 0
    if a.session:
        h, m = (int(x) for x in a.at.split(":"))
        now = dt.datetime.combine(dt.date.fromisoformat(a.session), dt.time(h, m), tzinfo=ET)
    else:
        now = dt.datetime.now(ET)

    st = eg.status(now)
    if not a.verbose and not st["in_window"]:
        return 0                                     # silent: no agent wake-up

    try:
        import market_context as mc
        regime = mc.regime_state().get("state", "unknown")
    except Exception:
        regime = "unknown"

    lookback = int(cfg.get("swing_lookback_sessions") or 10)
    h_start = now - dt.timedelta(days=int(lookback * 1.6) + 2)
    try:
        h_bars = fetch_bars(universe, "1Hour", h_start, now)
        m5_bars = fetch_bars(universe, "5Min", now.replace(hour=9, minute=30, second=0, microsecond=0), now)
    except Exception as e:
        print(f"ANCHOR SCAN {now:%H:%M} ET — could not fetch bars: {e}")
        return 0

    rows = [analyse(s, h_bars.get(s, []), m5_bars.get(s, []), cfg, now) for s in universe]
    if not a.verbose and not any(r["state"] in ("IN_ZONE", "ABSORPTION", "CONFIRMED") for r in rows):
        return 0                                     # silent: nothing in play
    print(render(now, st, regime, rows, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
