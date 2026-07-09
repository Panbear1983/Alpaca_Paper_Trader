"""
backtest_swing.py — historical validation of the swing_buyer + exit-engine rules
=================================================================================
Replays the combined swing strategy over daily bars (Alpaca iex feed):

  ENTRIES (weekly, every 5 trading days, only when SPY close > 50d SMA):
    rank universe by trailing 20d return minus SPY's → hold the top N with
    positive RS, equal-weight slots.
  EXITS (checked daily, mirroring capitol_copier.manage_open_positions):
    stop-loss  : close <= entry * (1 - stop_pct)
    trail-stop : after peak gain >= trail_trigger, close <= peak * (1 - giveback)
    rotate-out : at weekly eval, name fell below rank `exit_rank` (hysteresis —
                 enter top N, exit only when it drops out of top exit_rank)

  NO LOOKAHEAD: signals use day t's close; fills happen at day t+1's OPEN.
  Slippage: 5 bps per side.

Usage:
  python3 backtest_swing.py                  # 1-year backtest, default params
  python3 backtest_swing.py --days 500       # longer window
  python3 backtest_swing.py --top 6 --stop 0.08
"""

import argparse
import datetime as dt

from swing_buyer import fetch_daily_bars
from capitol_copier import load_config

SLIPPAGE = 0.0005  # 5 bps per side


def build_series(universe, days):
    """{sym: {date: bar}} plus the sorted union of SPY trading dates."""
    bars = fetch_daily_bars(universe + ["SPY"], days=days)
    series = {}
    for sym, blist in bars.items():
        series[sym] = {b["t"][:10]: b for b in blist}
    dates = sorted(series.get("SPY", {}).keys())
    return series, dates


def close_on(series, sym, d):
    b = series.get(sym, {}).get(d)
    return b["c"] if b else None


def open_on(series, sym, d):
    b = series.get(sym, {}).get(d)
    return b["o"] if b else None


def run_backtest(days=430, top_n=6, exit_rank=10, rs_lookback=20, sma_days=50,
                 stop_pct=0.08, trail_trigger=0.15, trail_giveback=0.08,
                 start_equity=100_000.0, verbose=False):
    cfg = load_config()
    universe = [s for s in cfg["intraday"]["universe"] if s != "TSLA"]
    series, dates = build_series(universe, days)

    warmup = max(sma_days, rs_lookback) + 2
    if len(dates) < warmup + 20:
        raise SystemExit(f"not enough SPY bars ({len(dates)}) — widen --days")

    cash      = start_equity
    positions = {}   # sym -> {qty, entry, peak}
    trades    = []   # closed round-trips
    curve     = []   # (date, equity)
    pending   = []   # orders decided at t's close, filled at t+1's open:
                     #   ("buy", sym, usd) | ("sell", sym, reason)

    def equity_at(d):
        eq = cash
        for sym, p in positions.items():
            c = close_on(series, sym, d)
            if c:
                eq += p["qty"] * c
        return eq

    spy_closes = []  # rolling for SMA

    for i, d in enumerate(dates):
        spy_c = close_on(series, "SPY", d)
        if spy_c:
            spy_closes.append(spy_c)

        # ── 1. Fill yesterday's decisions at TODAY's open ────────────────────
        nonlocal_cash = cash
        for order in pending:
            if order[0] == "sell":
                _, sym, reason = order
                if sym not in positions:
                    continue
                o = open_on(series, sym, d)
                if o is None:
                    continue
                p = positions.pop(sym)
                px = o * (1 - SLIPPAGE)
                nonlocal_cash += p["qty"] * px
                trades.append({
                    "sym": sym, "entry": p["entry"], "exit": px,
                    "ret": (px - p["entry"]) / p["entry"], "reason": reason,
                    "exit_date": d,
                })
            else:
                _, sym, usd = order
                o = open_on(series, sym, d)
                if o is None or usd > nonlocal_cash:
                    continue
                px = o * (1 + SLIPPAGE)
                positions[sym] = {"qty": usd / px, "entry": px, "peak": px}
                nonlocal_cash -= usd
        cash = nonlocal_cash
        pending = []

        if i < warmup:
            curve.append((d, equity_at(d)))
            continue

        # ── 2. Daily exit checks on today's close ────────────────────────────
        for sym, p in list(positions.items()):
            c = close_on(series, sym, d)
            if not c:
                continue
            p["peak"] = max(p["peak"], c)
            gain      = (c - p["entry"]) / p["entry"]
            peak_gain = (p["peak"] - p["entry"]) / p["entry"]
            if gain <= -stop_pct:
                pending.append(("sell", sym, "stop"))
            elif peak_gain >= trail_trigger and c <= p["peak"] * (1 - trail_giveback):
                pending.append(("sell", sym, "trail"))

        # ── 3. Weekly rebalance decision on today's close ────────────────────
        if (i - warmup) % 5 == 0:
            sma = sum(spy_closes[-sma_days:]) / sma_days
            risk_on = spy_closes[-1] >= sma

            # 20d RS vs SPY
            d_look = dates[i - rs_lookback]
            spy_then = close_on(series, "SPY", d_look)
            spy_ret = (spy_c - spy_then) / spy_then if (spy_c and spy_then) else 0.0
            ranked = []
            for sym in universe:
                c_now, c_then = close_on(series, sym, d), close_on(series, sym, d_look)
                if c_now and c_then:
                    ranked.append((sym, (c_now - c_then) / c_then - spy_ret))
            ranked.sort(key=lambda x: x[1], reverse=True)
            rank_of = {sym: r for r, (sym, _) in enumerate(ranked, 1)}
            queued_sells = {o[1] for o in pending if o[0] == "sell"}

            # rotate out anything that fell below exit_rank
            for sym in list(positions.keys()):
                if sym in queued_sells:
                    continue
                if rank_of.get(sym, 999) > exit_rank:
                    pending.append(("sell", sym, "rotate"))
                    queued_sells.add(sym)

            # enter top-N names (positive RS only) while slots + cash allow
            if risk_on:
                open_after = {s for s in positions if s not in queued_sells}
                est_eq  = equity_at(d)
                slot    = est_eq / top_n
                est_cash = cash + sum(
                    positions[s]["qty"] * (close_on(series, s, d) or 0)
                    for s in queued_sells if s in positions)
                for sym, rs in ranked[:top_n]:
                    if rs <= 0 or sym in open_after:
                        continue
                    if len(open_after) >= top_n or est_cash < slot:
                        break
                    pending.append(("buy", sym, slot))
                    open_after.add(sym)
                    est_cash -= slot

        curve.append((d, equity_at(d)))

    # ── Metrics ──────────────────────────────────────────────────────────────
    final_eq  = curve[-1][1]
    start_d   = dates[warmup]
    spy_start = close_on(series, "SPY", start_d)
    spy_end   = close_on(series, "SPY", dates[-1])
    spy_ret   = (spy_end - spy_start) / spy_start
    strat_ret = (final_eq - start_equity) / start_equity

    peak, max_dd = 0.0, 0.0
    for _, eq in curve[warmup:]:
        peak   = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    wins   = [t for t in trades if t["ret"] > 0]
    losses = [t for t in trades if t["ret"] <= 0]
    gross_win  = sum(t["ret"] for t in wins)
    gross_loss = abs(sum(t["ret"] for t in losses))
    pf = gross_win / gross_loss if gross_loss else float("inf")

    n_days = len(dates) - warmup
    years  = n_days / 252
    cagr   = (final_eq / start_equity) ** (1 / years) - 1 if years > 0 else 0

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    print("═" * 62)
    print(f"  SWING STRATEGY BACKTEST — {start_d} → {dates[-1]}  ({n_days} trading days)")
    print("═" * 62)
    print(f"  params: top{top_n} / exit-rank{exit_rank} / RS{rs_lookback}d / "
          f"SMA{sma_days} / stop -{stop_pct*100:.0f}% / trail +{trail_trigger*100:.0f}%/-{trail_giveback*100:.0f}%")
    print(f"  Strategy return : {strat_ret*100:+8.2f}%   (${start_equity:,.0f} → ${final_eq:,.0f})")
    print(f"  SPY buy & hold  : {spy_ret*100:+8.2f}%")
    print(f"  Alpha vs SPY    : {(strat_ret-spy_ret)*100:+8.2f}%")
    print(f"  CAGR            : {cagr*100:+8.2f}%")
    print(f"  Max drawdown    : {max_dd*100:8.2f}%")
    print(f"  Closed trades   : {len(trades)}   (open at end: {len(positions)})")
    if trades:
        wr = len(wins) / len(trades)
        avg = sum(t['ret'] for t in trades) / len(trades)
        print(f"  Win rate        : {wr*100:8.1f}%")
        print(f"  Avg trade       : {avg*100:+8.2f}%")
        print(f"  Profit factor   : {pf:8.2f}")
        print(f"  Exit breakdown  : {reasons}")
    print("═" * 62)

    if verbose and trades:
        print(f"\n  {'SYM':<6} {'RET':>8} {'REASON':<8} {'EXIT DATE'}")
        for t in trades:
            print(f"  {t['sym']:<6} {t['ret']*100:>+7.2f}% {t['reason']:<8} {t['exit_date']}")

    return {"strat_ret": strat_ret, "spy_ret": spy_ret, "max_dd": max_dd,
            "n_trades": len(trades), "pf": pf if trades else None,
            "win_rate": len(wins) / len(trades) if trades else None}


def main():
    import urllib3
    urllib3.disable_warnings()
    ap = argparse.ArgumentParser(description="Backtest the swing RS strategy")
    ap.add_argument("--days", type=int, default=430, help="calendar days of history")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--exit-rank", type=int, default=10)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--trail-trigger", type=float, default=0.15)
    ap.add_argument("--trail-giveback", type=float, default=0.08)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    run_backtest(days=args.days, top_n=args.top, exit_rank=args.exit_rank,
                 stop_pct=args.stop, trail_trigger=args.trail_trigger,
                 trail_giveback=args.trail_giveback, verbose=args.verbose)


if __name__ == "__main__":
    main()
