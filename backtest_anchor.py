#!/usr/bin/env python3
"""
backtest_anchor.py — the Phase 2 gate for the anchor entry rule (2026-09-23)
============================================================================
Before the High Risk wallet is handed back to Wanna Buffet, the rule it would
trade has to earn it. This replays that rule on daily bars (Alpaca IEX feed)
over the 15-name allow list, inside the same box the wallet lives in today:

  ENTRY (signal on day t's close, filled at day t+1's OPEN, 5 bps slippage)
    pullback : close <= (1 - pullback) x highest close of the previous
               `lookback` sessions (default 5% under the 20-day high)
    bounce   : close >= (1 + bounce) x lowest low since that high was set
               (default 1.5% off the low) AND an up day (close > prior close)
    a name stopped out is not re-entered for `cooldown` sessions

  EXIT    a trailing stop from the entry, per name: width = clamp(atr_mult x
          ATR14%, stop_min, stop_max) — the same widths broker_stops.py rests
          at Alpaca. The high-water mark tracks the daily HIGH; the stop
          fills at the stop price, or at the open when the day gaps below it.

  BOX     size = `size` of equity per entry, capped at `name_cap` per name,
          at most `max_names` open, cash never below `cash_floor` of equity,
          no borrowing. Equal to the High Risk guardrails of 2026-09-22.

  GATE    over the trailing 252 sessions the rule must match or beat QQQ
          buy-and-hold AND have profit factor > 1. Fails either → no hand-back.

Usage:
  python3 backtest_anchor.py                # 430 calendar days, defaults
  python3 backtest_anchor.py --verbose      # every trade
  python3 backtest_anchor.py --pullback 0.07 --bounce 0.02 --size 0.20
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics

SLIPPAGE = 0.0005
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = dict(pullback=0.05, bounce=0.015, lookback=20, cooldown=5,
                size=0.20, name_cap=0.25, max_names=5, cash_floor=0.20,
                atr_mult=2.5, stop_min=0.06, stop_max=0.14, atr_days=14,
                window=252, start_equity=100_000.0)


# ── pure pieces (unit-tested) ────────────────────────────────────────────────

def atr_pct(bars: list[dict], i: int, n: int) -> float | None:
    """ATR over the n bars ending at i, as a fraction of bar i's close."""
    if i < n or not bars[i].get("c"):
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l, pc = bars[k]["h"], bars[k]["l"], bars[k - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.mean(trs) / bars[i]["c"]


def stop_width(atr: float | None, mult: float, lo: float, hi: float) -> float:
    """clamp(mult x ATR%, lo, hi); no ATR → the widest allowed (fewest false stops)."""
    if atr is None:
        return hi
    return min(hi, max(lo, mult * atr))


def signal(bars: list[dict], i: int, lookback: int, pullback: float, bounce: float) -> dict | None:
    """The entry setup on bar i's close, or None.

    Returns {"hh", "ll", "depth", "bounce"} when: bar i closes at least
    `pullback` under the highest close of bars [i-lookback, i-1], has bounced
    at least `bounce` off the lowest low since that high, and is an up day.
    """
    if i < lookback + 1:
        return None
    window = bars[i - lookback:i]
    hh = max(b["c"] for b in window)
    hh_idx = i - lookback + max(range(len(window)), key=lambda k: window[k]["c"])
    c = bars[i]["c"]
    if not c or c > hh * (1 - pullback):
        return None
    ll = min(b["l"] for b in bars[hh_idx:i + 1])
    if c < ll * (1 + bounce) or c <= bars[i - 1]["c"]:
        return None
    return {"hh": hh, "ll": ll, "depth": 1 - c / hh, "bounce": c / ll - 1}


def stop_fill(bar: dict, stop: float) -> float | None:
    """Price the stop fills at during `bar`, or None if untouched. A gap
    below the stop fills at the open, like a real stop order."""
    if bar["o"] <= stop:
        return bar["o"]
    if bar["l"] <= stop:
        return stop
    return None


# ── the replay ───────────────────────────────────────────────────────────────

def build_series(universe: list[str], days: int, bench: str = "QQQ"):
    from swing_buyer import fetch_daily_bars
    raw = fetch_daily_bars(sorted(set(universe + [bench])), days=days)
    series = {sym: {b["t"][:10]: b for b in blist} for sym, blist in raw.items()}
    dates = sorted(series.get(bench, {}).keys())
    return series, dates


def simulate(series: dict, dates: list[str], universe: list[str], p: dict, verbose: bool = False):
    """Returns (curve [(date, equity, invested_frac)], trades [dict])."""
    # per-symbol bar lists aligned to their own trading days, with an index by date
    bars = {s: sorted(series.get(s, {}).values(), key=lambda b: b["t"]) for s in universe}
    idx = {s: {b["t"][:10]: k for k, b in enumerate(bl)} for s, bl in bars.items()}
    cash = float(p["start_equity"])
    positions: dict[str, dict] = {}
    pending: list[dict] = []
    cooldown_until: dict[str, int] = {}
    trades: list[dict] = []
    curve: list[tuple[str, float, float]] = []

    def mv_on(d):
        return sum(pos["qty"] * series[s][d]["c"] for s, pos in positions.items() if d in series.get(s, {}))

    for di, d in enumerate(dates):
        equity = curve[-1][1] if curve else cash          # size on yesterday's close, no peeking
        # 1. yesterday's decisions fill at today's open
        for o in pending:
            s = o["sym"]
            if s in positions or d not in series.get(s, {}):
                continue
            held_names = len(positions)
            if held_names >= p["max_names"]:
                continue
            usd = min(p["size"] * equity, p["name_cap"] * equity, cash - p["cash_floor"] * equity)
            if usd < 100:
                continue
            px = series[s][d]["o"] * (1 + SLIPPAGE)
            qty = usd / px
            positions[s] = {"qty": qty, "entry": px, "hwm": px, "width": o["width"],
                            "stop": px * (1 - o["width"]), "entry_date": d, "usd": usd}
            cash -= usd
            if verbose:
                print(f"  {d} BUY  {s:<5} ${usd:>8,.0f} @ {px:8.2f}  stop {o['width']*100:4.1f}%  (depth {o['depth']*100:.1f}%)")
        pending = []

        # 2. stops during today's session (from the PRIOR high-water mark), then ratchet
        for s, pos in list(positions.items()):
            if d not in series.get(s, {}):
                continue
            bar = series[s][d]
            fill = stop_fill(bar, pos["stop"])
            if fill is not None:
                px = fill * (1 - SLIPPAGE)
                cash += pos["qty"] * px
                trades.append({"sym": s, "entry": pos["entry"], "exit": px, "usd": pos["usd"],
                               "pnl": pos["qty"] * (px - pos["entry"]), "ret": px / pos["entry"] - 1,
                               "entry_date": pos["entry_date"], "exit_date": d,
                               "hold": di - dates.index(pos["entry_date"]) if pos["entry_date"] in dates else 0})
                if verbose:
                    print(f"  {d} STOP {s:<5} @ {px:8.2f}  {trades[-1]['ret']*100:+6.2f}%  ${trades[-1]['pnl']:+,.0f}")
                positions.pop(s)
                cooldown_until[s] = di + p["cooldown"]
                continue
            if bar["h"] > pos["hwm"]:
                pos["hwm"] = bar["h"]
                pos["stop"] = pos["hwm"] * (1 - pos["width"])

        # 3. signals on today's close → orders for tomorrow's open
        slots = p["max_names"] - len(positions)
        if slots > 0:
            cands = []
            for s in universe:
                if s in positions or cooldown_until.get(s, -1) >= di:
                    continue
                k = idx.get(s, {}).get(d)
                if k is None:
                    continue
                sig = signal(bars[s], k, p["lookback"], p["pullback"], p["bounce"])
                if not sig:
                    continue
                w = stop_width(atr_pct(bars[s], k, p["atr_days"]), p["atr_mult"], p["stop_min"], p["stop_max"])
                cands.append({"sym": s, "width": w, **sig})
            cands.sort(key=lambda c: -c["bounce"])
            pending = cands[:slots]

        eq = cash + mv_on(d)
        curve.append((d, eq, (eq - cash) / eq if eq else 0.0))
    return curve, trades


def metrics(curve, trades, bench_closes: dict, window: int) -> dict:
    """Full-period and trailing-window figures, plus the gate verdict."""
    def span(c):
        start, end = c[0], c[-1]
        peak, mdd = -1e18, 0.0
        for _, eq, _ in c:
            peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        b0, b1 = bench_closes.get(start[0]), bench_closes.get(end[0])
        ts = [t for t in trades if start[0] <= t["exit_date"] <= end[0]]
        gp = sum(t["pnl"] for t in ts if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in ts if t["pnl"] <= 0))
        return {"from": start[0], "to": end[0], "ret": end[1] / start[1] - 1,
                "bench_ret": (b1 / b0 - 1) if b0 and b1 else None, "max_dd": mdd,
                "trades": len(ts), "wins": sum(1 for t in ts if t["pnl"] > 0),
                "pf": (gp / gl) if gl else (math.inf if gp else 0.0),
                "avg_hold": statistics.mean([t["hold"] for t in ts]) if ts else 0.0,
                "avg_invested": statistics.mean([x[2] for x in c]) if c else 0.0}
    full = span(curve)
    tail = span(curve[-window:]) if len(curve) > window else full
    gate = (tail["bench_ret"] is not None and tail["ret"] >= tail["bench_ret"] and tail["pf"] > 1.0)
    return {"full": full, "window": tail, "gate_pass": gate}


def fmt(m: dict) -> str:
    def row(name, s):
        b = f"{s['bench_ret']*100:+.1f}%" if s["bench_ret"] is not None else "n/a"
        return (f"{name:<22} {s['from']} → {s['to']}: rule {s['ret']*100:+.1f}%  QQQ {b}  "
                f"maxDD {s['max_dd']*100:.1f}%  trades {s['trades']} (win {s['wins']})  "
                f"PF {s['pf']:.2f}  hold {s['avg_hold']:.1f}d  invested {s['avg_invested']*100:.0f}%")
    return "\n".join([row("full period", m["full"]), row("gate window (252 sess)", m["window"]),
                      f"GATE: {'PASS' if m['gate_pass'] else 'FAIL'} — needs rule ≥ QQQ and PF > 1 over the window"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the anchor pullback-and-bounce rule inside the High Risk box")
    ap.add_argument("--days", type=int, default=430)
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--json", help="also write the figures to this path")
    a = ap.parse_args()
    p = {k: getattr(a, k) for k in DEFAULTS}
    import strategies
    universe = [str(s).upper() for s in (strategies.load_merged("High Risk").get("anchor") or {}).get("universe") or []]
    if not universe:
        print("no anchor universe configured"); return 1
    series, dates = build_series(universe, a.days)
    short = [s for s in universe if len(series.get(s, {})) < p["lookback"] + p["atr_days"] + 5]
    curve, trades = simulate(series, dates, universe, p, verbose=a.verbose)
    bench = {d: b["c"] for d, b in series.get("QQQ", {}).items()}
    m = metrics(curve, trades, bench, p["window"])
    print(f"universe {len(universe)} names, {len(dates)} sessions, params {json.dumps(p)}")
    if short:
        print(f"note: {', '.join(short)} have little history and could only trade once they had enough bars")
    print(fmt(m))
    by = {}
    for t in trades:
        by.setdefault(t["sym"], []).append(t["pnl"])
    if by:
        print("per name: " + "  ".join(f"{s} {sum(v):+,.0f}/{len(v)}" for s, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"params": p, "universe": universe, "metrics": m, "trades": trades,
                       "run_at": dt.datetime.now().isoformat(timespec="seconds")}, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
