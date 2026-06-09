#!/usr/bin/env python3
"""
hermes_report.py — Alpaca paper-trading daily report → Telegram.

Sections:
  1. Account Summary  (equity, day P&L, SPY alpha)
  2. Portfolio Holdings  (all open positions: avg cost, current price, P&L per share and total)
  3. Today's Purchases  (filled buys from today ET)
  4. Watchlist Quotes
  Charts: 30-day equity curve + allocation pie

Run manually:
    python3 hermes_report.py             # generate + send to Telegram
    python3 hermes_report.py --dry-run   # generate only, print to stdout

Env reads from .env in this folder, then ~/.hermes/.env:
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
    TELEGRAM_BOT_TOKEN, TELEGRAM_HOME_CHANNEL
    WATCHLIST   (comma-separated tickers, default: SPY,QQQ,TSLA,NVDA,AAPL)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from dotenv import load_dotenv

# ── env ───────────────────────────────────────────────────────────────────────
HERE        = Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

load_dotenv(HERE / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

ALPACA_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE   = os.environ.get("ALPACA_BASE_URL",
                               "https://paper-api.alpaca.markets/v2").rstrip("/")
ALPACA_DATA   = "https://data.alpaca.markets/v2"
TG_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
WATCHLIST     = [s.strip() for s in
                 os.environ.get("WATCHLIST", "SPY,QQQ,TSLA,NVDA,AAPL").split(",")]

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Accept":              "application/json",
}

ET = dt.timezone(dt.timedelta(hours=-4))   # ET (no DST correction needed for gate logic)


# ── Alpaca fetch helpers ───────────────────────────────────────────────────────
def _get(url: str, **params: Any) -> Any:
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_account() -> dict:
    return _get(f"{ALPACA_BASE}/account")


def fetch_positions() -> list[dict]:
    result = _get(f"{ALPACA_BASE}/positions")
    return result if isinstance(result, list) else []


def fetch_orders_today_buys() -> list[dict]:
    """Filled buy orders placed on today's ET date."""
    today_et  = dt.datetime.now(ET).date().isoformat()
    after_iso = f"{today_et}T00:00:00-04:00"
    try:
        orders = _get(
            f"{ALPACA_BASE}/orders",
            status="closed",
            limit=200,
            direction="desc",
            after=after_iso,
        )
        if not isinstance(orders, list):
            return []
        return [
            o for o in orders
            if o.get("side") == "buy" and float(o.get("filled_qty") or 0) > 0
        ]
    except Exception as exc:
        print(f"[warn] today_buys fetch: {exc}", file=sys.stderr)
        return []


def fetch_portfolio_history(period: str = "1M", timeframe: str = "1D") -> dict:
    return _get(f"{ALPACA_BASE}/account/portfolio/history",
                period=period, timeframe=timeframe)


def fetch_quotes(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    try:
        r = _get(f"{ALPACA_DATA}/stocks/quotes/latest",
                 symbols=",".join(symbols))
        return r.get("quotes", {})
    except Exception as exc:
        return {"_error": str(exc)}


def fetch_spy_day_pct() -> float | None:
    """SPY % change open→current (or open→close) for today."""
    try:
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        r = requests.get(
            f"{ALPACA_DATA}/stocks/SPY/bars",
            headers=HEADERS,
            params={"timeframe": "1Day", "start": today, "limit": 1},
            timeout=15,
        )
        bars = r.json().get("bars", [])
        if bars:
            b = bars[0]
            return (b["c"] - b["o"]) / b["o"] * 100
    except Exception:
        pass
    return None


# ── formatting helpers ────────────────────────────────────────────────────────
def _m(x: Any) -> str:
    """Dollar amount, no sign."""
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "—"


def _s(x: Any) -> str:
    """Signed dollar amount."""
    try:
        v = float(x)
        return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    except Exception:
        return "—"


def _p(x: Any) -> str:
    """Signed percentage (value already in %, e.g. 1.23 → '+1.23%')."""
    try:
        v = float(x)
        return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"
    except Exception:
        return "—"


def _pp(x: Any) -> str:
    """Signed percentage from decimal (e.g. 0.0123 → '+1.23%')."""
    try:
        return _p(float(x) * 100)
    except Exception:
        return "—"


def _qty(q: Any) -> str:
    """Display quantity with sensible precision."""
    try:
        v = float(q)
        if v >= 100:  return f"{v:.1f}"
        if v >= 10:   return f"{v:.2f}"
        if v >= 1:    return f"{v:.3f}"
        return f"{v:.4f}"
    except Exception:
        return "—"


# ── report ────────────────────────────────────────────────────────────────────
def build_report(
    acct:          dict,
    positions:     list[dict],
    today_buys:    list[dict],
    quotes:        dict,
    spy_pct:       float | None,
) -> str:
    now_str  = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    equity   = float(acct.get("equity",      0))
    last_eq  = float(acct.get("last_equity", equity))
    cash     = float(acct.get("cash",        0))
    bp       = float(acct.get("buying_power",0))
    day_pnl  = equity - last_eq
    day_pct  = (day_pnl / last_eq * 100) if last_eq else 0.0

    L: list[str] = []

    # ── header ─────────────────────────────────────────────────────────────
    L.append("📊 *Alpaca Paper-Trading Report*")
    L.append(f"_{now_str}_\n")

    # ── 1. account summary ─────────────────────────────────────────────────
    L.append("*Account*")
    L.append(f"• Equity:        `{_m(equity)}`")
    L.append(f"• Cash:          `{_m(cash)}`")
    L.append(f"• Buying Power:  `{_m(bp)}`")
    L.append(f"• Day P&L:       `{_s(day_pnl)}  ({_p(day_pct)})`")
    if spy_pct is not None:
        alpha = day_pct - spy_pct
        L.append(f"• SPY today:     `{_p(spy_pct)}`   alpha vs SPY: `{_p(alpha)}`")
    L.append("")

    # ── 2. portfolio holdings ──────────────────────────────────────────────
    if positions:
        # Alpaca provides cost_basis = avg_entry_price × qty (accurate for all fills)
        # unrealized_pl  = market_value − cost_basis
        # unrealized_plpc = unrealized_pl / cost_basis  (decimal)
        positions_sorted = sorted(
            positions,
            key=lambda p: float(p.get("unrealized_pl", 0)),
            reverse=True,
        )

        total_cost   = sum(float(p.get("cost_basis",   0)) for p in positions)
        total_mktval = sum(float(p.get("market_value", 0)) for p in positions)
        total_unreal = total_mktval - total_cost
        total_pct    = (total_unreal / total_cost * 100) if total_cost else 0.0

        L.append(f"*Holdings ({len(positions)} positions)*")
        L.append("```")
        # Column headers
        L.append(f"{'SYM':<6} {'QTY':>8} {'AVG':>9} {'PRICE':>9} {'P&L':>10} {'%':>8}")
        L.append("─" * 56)
        for p in positions_sorted:
            sym   = p["symbol"]
            qty   = float(p.get("qty",              0))
            avg   = float(p.get("avg_entry_price",  0))
            cur   = float(p.get("current_price",    0))
            upl   = float(p.get("unrealized_pl",    0))
            uplpc = float(p.get("unrealized_plpc",  0)) * 100
            arrow = "▲" if upl >= 0 else "▼"
            L.append(
                f"{sym:<6} {_qty(qty):>8} ${avg:>8.2f} ${cur:>8.2f}"
                f" {upl:>+9.2f} {arrow}{abs(uplpc):>6.2f}%"
            )
        L.append("─" * 56)
        oa = "▲" if total_unreal >= 0 else "▼"
        L.append(
            f"{'TOTAL':<6} {'':>8} {'':>9} {_m(total_mktval):>9}"
            f" {total_unreal:>+9.2f} {oa}{abs(total_pct):>6.2f}%"
        )
        L.append("```")
        L.append(f"• Cost basis:   `{_m(total_cost)}`")
        L.append(f"• Market value: `{_m(total_mktval)}`")
        L.append(f"• Unrealized:   `{_s(total_unreal)}  ({_p(total_pct)})`")
        L.append("")
    else:
        L.append("*Holdings*\n_(no open positions)_\n")

    # ── 3. today's purchases ───────────────────────────────────────────────
    today_label = dt.datetime.now(ET).strftime("%Y-%m-%d")
    L.append(f"*Purchases — {today_label}*")
    if today_buys:
        L.append("```")
        L.append(f"{'TIME (ET)':>8} {'SYM':<6} {'QTY':>8} {'@ PRICE':>9} {'COST':>10}")
        L.append("─" * 46)
        day_spend = 0.0
        for o in today_buys:
            sym     = o.get("symbol", "?")
            qty     = float(o.get("filled_qty", 0))
            fill_px = float(o.get("filled_avg_price") or 0)
            cost    = qty * fill_px
            day_spend += cost
            # convert UTC fill time to ET
            raw_t = o.get("filled_at") or ""
            try:
                utc_t = dt.datetime.fromisoformat(raw_t.replace("Z", "+00:00"))
                et_t  = utc_t.astimezone(ET).strftime("%H:%M")
            except Exception:
                et_t = raw_t[11:16] if len(raw_t) >= 16 else "—"
            L.append(
                f"{et_t:>8} {sym:<6} {_qty(qty):>8} ${fill_px:>8.2f} ${cost:>9.2f}"
            )
        L.append("─" * 46)
        L.append(f"{'':>8} {'TOTAL':<6} {'':>8} {'':>9} ${day_spend:>9.2f}")
        L.append("```")
    else:
        L.append("_(no buys filled today)_")
    L.append("")

    # ── 4. watchlist ───────────────────────────────────────────────────────
    L.append("*Watchlist*")
    if isinstance(quotes, dict) and "_error" in quotes:
        L.append(f"_quotes unavailable: {quotes['_error']}_")
    elif not quotes:
        L.append("_(no quotes)_")
    else:
        L.append("```")
        for sym in WATCHLIST:
            q       = quotes.get(sym) or {}
            bid, ask = q.get("bp"), q.get("ap")
            mid     = ((bid + ask) / 2) if (bid and ask) else None
            mid_s   = f"{mid:.2f}" if mid else "—"
            bid_s   = f"{bid:.2f}" if bid else "—"
            ask_s   = f"{ask:.2f}" if ask else "—"
            L.append(f"{sym:<6}  mid {mid_s:>9}  bid {bid_s:>9}  ask {ask_s:>9}")
        L.append("```")

    L.append("\n_Hermes daily-report cron_")
    return "\n".join(L)


# ── charts ────────────────────────────────────────────────────────────────────
def chart_equity(history: dict, out: Path) -> Path | None:
    eq = history.get("equity") or []
    ts = history.get("timestamp") or []
    if not eq or not ts:
        return None
    times = [dt.datetime.fromtimestamp(t) for t in ts]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(times, eq, linewidth=1.8, color="#1f77b4")
    ax.fill_between(times, eq, alpha=0.12, color="#1f77b4")
    ax.set_title("Equity — last 30 days")
    ax.set_ylabel("USD")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def chart_allocation(positions: list[dict], out: Path) -> Path | None:
    if not positions:
        return None
    sizes  = [abs(float(p.get("market_value", 0))) for p in positions]
    labels = [p["symbol"] for p in positions]
    total  = sum(sizes) or 1
    # suppress labels on tiny slices to keep the chart readable
    disp_labels = [lbl if (sz / total) > 0.025 else "" for lbl, sz in zip(labels, sizes)]
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.pie(
        sizes,
        labels=disp_labels,
        autopct=lambda pct: f"{pct:.1f}%" if pct > 2.5 else "",
        startangle=90,
        textprops={"fontsize": 8},
    )
    ax.set_title("Allocation by market value")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_send_text(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        print("[tg] skip — TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL not set",
              file=sys.stderr)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"[tg] text error {r.status_code}: {r.text}", file=sys.stderr)


def tg_send_photo(path: Path, caption: str = "") -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    with open(path, "rb") as fh:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT, "caption": caption},
            files={"photo": fh},
            timeout=60,
        )
    if not r.ok:
        print(f"[tg] photo error {r.status_code}: {r.text}", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Alpaca daily report → Telegram")
    ap.add_argument("--dry-run", action="store_true",
                    help="Generate report files but skip Telegram push")
    args = ap.parse_args()

    stamp     = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path   = REPORTS_DIR / f"alpaca_{stamp}.md"
    eq_png    = REPORTS_DIR / f"alpaca_equity_{stamp}.png"
    alloc_png = REPORTS_DIR / f"alpaca_alloc_{stamp}.png"

    print("[1/5] Fetching account...",              flush=True)
    acct = fetch_account()
    print("[2/5] Fetching positions...",             flush=True)
    positions = fetch_positions()
    print("[3/5] Fetching today's buy orders...",   flush=True)
    today_buys = fetch_orders_today_buys()
    print("[4/5] Fetching history, quotes, SPY...", flush=True)
    history  = fetch_portfolio_history()
    quotes   = fetch_quotes(WATCHLIST)
    spy_pct  = fetch_spy_day_pct()
    print("[5/5] Building report + charts...",      flush=True)

    report = build_report(acct, positions, today_buys, quotes, spy_pct)
    md_path.write_text(report, encoding="utf-8")

    eq_done    = chart_equity(history, eq_png)
    alloc_done = chart_allocation(positions, alloc_png)

    print(f"\n✓ Report:       {md_path}")
    if eq_done:    print(f"✓ Equity chart: {eq_png}")
    if alloc_done: print(f"✓ Alloc chart:  {alloc_png}")

    if args.dry_run:
        print("\n--- DRY RUN (Telegram skipped) ---\n")
        print(report)
        return 0

    print("\n[tg] Sending report text...")
    tg_send_text(report)
    if eq_done:
        print("[tg] Sending equity chart...")
        tg_send_photo(eq_png, "Equity — last 30 days")
    if alloc_done:
        print("[tg] Sending allocation chart...")
        tg_send_photo(alloc_png, "Allocation by market value")

    print("✓ Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as exc:
        print(f"\n✗ HTTP {exc.response.status_code}: {exc.response.text}",
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
