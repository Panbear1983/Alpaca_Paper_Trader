#!/usr/bin/env python3
"""
daily_metrics.py — Day-to-Day Trading Performance & Win/Loss Ratio Tracker
==========================================================================
Measures day-by-day performance over monthly (or custom) horizons:
  - Green Days (Wins) vs Red Days (Losses) count and percentages
  - Win / Loss Days Ratio (Win Days : Loss Days)
  - Daily Profit Factor (Total Daily Gains / Total Daily Losses)
  - Average Gain on Win Days vs Average Loss on Red Days (Payoff Ratio)
  - Consecutive Win/Loss Day Streaks
  - Recent Day-by-Day Historical Log

Can be used standalone via CLI or imported into reports and the TUI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import requests

import wallets

ET = ZoneInfo("America/New_York")


def compute_daily_metrics(timestamps: list[int], equities: list[float]) -> dict[str, Any]:
    """Compute deterministic daily win/loss performance metrics from consecutive closes."""
    # Filter out initial unfunded / zero days
    valid_points = [(ts, eq) for ts, eq in zip(timestamps, equities) if eq is not None and eq > 0]
    
    if len(valid_points) < 2:
        return {
            "total_days": 0,
            "win_days": 0,
            "loss_days": 0,
            "flat_days": 0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "win_loss_ratio": 0.0,
            "total_gains": 0.0,
            "total_losses": 0.0,
            "net_pnl": 0.0,
            "net_pnl_pct": 0.0,
            "avg_win_dollar": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_dollar": 0.0,
            "avg_loss_pct": 0.0,
            "daily_profit_factor": 0.0,
            "payoff_ratio": 0.0,
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "current_streak": {"type": "none", "length": 0},
            "days": [],
        }

    daily_records = []
    for i in range(1, len(valid_points)):
        prev_ts, prev_eq = valid_points[i - 1]
        cur_ts, cur_eq = valid_points[i]
        diff = cur_eq - prev_eq
        pct = (diff / prev_eq) * 100.0
        d_str = dt.datetime.fromtimestamp(cur_ts, dt.timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        daily_records.append({
            "date": d_str,
            "diff": round(diff, 2),
            "pct": round(pct, 2),
            "equity": round(cur_eq, 2),
        })

    win_days = [r for r in daily_records if r["diff"] > 0]
    loss_days = [r for r in daily_records if r["diff"] < 0]
    flat_days = [r for r in daily_records if r["diff"] == 0]

    n_total = len(daily_records)
    n_win = len(win_days)
    n_loss = len(loss_days)
    n_flat = len(flat_days)

    win_rate = (n_win / n_total * 100.0) if n_total > 0 else 0.0
    loss_rate = (n_loss / n_total * 100.0) if n_total > 0 else 0.0

    if n_loss > 0:
        win_loss_ratio = round(n_win / n_loss, 2)
    elif n_win > 0:
        win_loss_ratio = float("inf")
    else:
        win_loss_ratio = 0.0

    total_gains = sum(r["diff"] for r in win_days)
    total_losses = abs(sum(r["diff"] for r in loss_days))
    net_pnl = valid_points[-1][1] - valid_points[0][1]
    net_pnl_pct = (net_pnl / valid_points[0][1]) * 100.0

    avg_win_dollar = (total_gains / n_win) if n_win > 0 else 0.0
    avg_win_pct = (sum(r["pct"] for r in win_days) / n_win) if n_win > 0 else 0.0

    avg_loss_dollar = (total_losses / n_loss) if n_loss > 0 else 0.0
    avg_loss_pct = (abs(sum(r["pct"] for r in loss_days)) / n_loss) if n_loss > 0 else 0.0

    if total_losses > 0:
        daily_profit_factor = round(total_gains / total_losses, 2)
    elif total_gains > 0:
        daily_profit_factor = float("inf")
    else:
        daily_profit_factor = 0.0

    if avg_loss_dollar > 0:
        payoff_ratio = round(avg_win_dollar / avg_loss_dollar, 2)
    elif avg_win_dollar > 0:
        payoff_ratio = float("inf")
    else:
        payoff_ratio = 0.0

    # Calculate streaks
    max_win_streak = 0
    max_loss_streak = 0
    cur_streak_type = "none"
    cur_streak_len = 0

    for r in daily_records:
        if r["diff"] > 0:
            if cur_streak_type == "win":
                cur_streak_len += 1
            else:
                cur_streak_type = "win"
                cur_streak_len = 1
            max_win_streak = max(max_win_streak, cur_streak_len)
        elif r["diff"] < 0:
            if cur_streak_type == "loss":
                cur_streak_len += 1
            else:
                cur_streak_type = "loss"
                cur_streak_len = 1
            max_loss_streak = max(max_loss_streak, cur_streak_len)
        else:
            cur_streak_type = "flat"
            cur_streak_len = 1

    return {
        "total_days": n_total,
        "win_days": n_win,
        "loss_days": n_loss,
        "flat_days": n_flat,
        "win_rate": round(win_rate, 1),
        "loss_rate": round(loss_rate, 1),
        "win_loss_ratio": win_loss_ratio,
        "total_gains": round(total_gains, 2),
        "total_losses": round(total_losses, 2),
        "net_pnl": round(net_pnl, 2),
        "net_pnl_pct": round(net_pnl_pct, 2),
        "avg_win_dollar": round(avg_win_dollar, 2),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_dollar": round(avg_loss_dollar, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "daily_profit_factor": daily_profit_factor,
        "payoff_ratio": payoff_ratio,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "current_streak": {"type": cur_streak_type, "length": cur_streak_len},
        "start_equity": round(valid_points[0][1], 2),
        "end_equity": round(valid_points[-1][1], 2),
        "days": daily_records,
    }


def fetch_daily_metrics(wallet_name: str, period: str = "1M") -> dict[str, Any] | None:
    """Fetch history from Alpaca and compute metrics for `wallet_name`."""
    creds = wallets.resolve(wallet_name)
    if not creds:
        return None
    key, secret, base = creds
    try:
        r = requests.get(
            f"{base}/account/portfolio/history",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={"period": period, "timeframe": "1D"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        timestamps = data.get("timestamp") or []
        equities = data.get("equity") or []
        metrics = compute_daily_metrics(timestamps, equities)
        metrics["wallet"] = wallet_name
        metrics["period"] = period
        return metrics
    except Exception:
        return None


def format_daily_summary(metrics: dict[str, Any], show_table: bool = True) -> str:
    """Format metrics into a concise, readable diagnostic block."""
    if not metrics or metrics.get("total_days", 0) == 0:
        return "No daily trading history available."

    wallet = metrics.get("wallet", "Account")
    period = metrics.get("period", "")
    n_total = metrics["total_days"]
    n_win = metrics["win_days"]
    n_loss = metrics["loss_days"]
    n_flat = metrics["flat_days"]
    win_rate = metrics["win_rate"]
    loss_rate = metrics["loss_rate"]
    wl_ratio = metrics["win_loss_ratio"]
    dpf = metrics["daily_profit_factor"]
    payoff = metrics["payoff_ratio"]
    net_pnl = metrics["net_pnl"]
    net_pct = metrics["net_pnl_pct"]
    avg_win = metrics["avg_win_dollar"]
    avg_win_p = metrics["avg_win_pct"]
    avg_loss = metrics["avg_loss_dollar"]
    avg_loss_p = metrics["avg_loss_pct"]
    cur_stk = metrics["current_streak"]

    sign = "+" if net_pnl >= 0 else "-"
    lines = [
        f"📅 Daily Win/Loss Performance — {wallet} ({period}, {n_total} trading days)",
        f"Equity: ${metrics['start_equity']:,.2f} ➔ ${metrics['end_equity']:,.2f}  |  Net: {sign}${abs(net_pnl):,.2f} ({net_pct:+.2f}%)",
        "",
        f"  • Profit Days (Green):   {n_win:2d} days ({win_rate:4.1f}%)",
        f"  • Loss Days (Red):       {n_loss:2d} days ({loss_rate:4.1f}%)" + (f"  [{n_flat} flat]" if n_flat else ""),
        f"  • Win / Loss Days Ratio: {wl_ratio:.2f} : 1",
        f"  • Daily Profit Factor:   {dpf:.2f}  (Total Gains: ${metrics['total_gains']:,.2f} / Total Losses: ${metrics['total_losses']:,.2f})",
        f"  • Avg Win vs Loss Day:   +${avg_win:,.2f} (+{avg_win_p:.2f}%)  vs  -${avg_loss:,.2f} (-{avg_loss_p:.2f}%)  [Payoff: {payoff:.2f}x]",
        f"  • Streaks:               Max Win: {metrics['max_win_streak']}d · Max Loss: {metrics['max_loss_streak']}d · Current: {cur_stk['length']} {cur_stk['type']} day(s)",
    ]

    if show_table and metrics.get("days"):
        lines.append("")
        lines.append("Recent 5 Sessions:")
        for d in metrics["days"][-5:]:
            dsign = "+" if d["diff"] >= 0 else "-"
            icon = "🟢" if d["diff"] > 0 else ("🔴" if d["diff"] < 0 else "⚪")
            lines.append(f"  {icon} {d['date']}: {dsign}${abs(d['diff']):8,.2f} ({d['pct']:+5.2f}%)  [Close: ${d['equity']:,.2f}]")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Day-to-Day Trading Performance Tracker.")
    parser.add_argument("--wallet", default="Wanna_Buffet Auto Trading", help="Target wallet name")
    parser.add_argument("--period", default="1M", choices=["1M", "3M", "1A"], help="Historical horizon (1M, 3M, 1A)")
    parser.add_argument("--all-days", action="store_true", help="Print complete daily log table")
    args = parser.parse_args()

    # Rebind wallet globals
    wallets.apply(args.wallet)
    metrics = fetch_daily_metrics(args.wallet, period=args.period)
    if not metrics:
        print(f"Error: Unable to fetch daily history for '{args.wallet}'. Check credentials.")
        return 1

    print(format_daily_summary(metrics, show_table=True))

    if args.all_days and metrics.get("days"):
        print("\nFull Day-by-Day History:")
        print(f"{'Date':<12} {'Diff ($)':<12} {'Return (%)':<12} {'Close Equity ($)':<16}")
        print("-" * 54)
        for d in metrics["days"]:
            dsign = "+" if d["diff"] >= 0 else "-"
            diff_str = f"{dsign}${abs(d['diff']):,.2f}"
            pct_str = f"{d['pct']:+.2f}%"
            eq_str = f"${d['equity']:,.2f}"
            print(f"{d['date']:<12} {diff_str:<12} {pct_str:<12} {eq_str:<16}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
