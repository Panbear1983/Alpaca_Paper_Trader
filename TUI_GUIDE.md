# Alpaca Paper Trader — TUI Cockpit Guide

A live Textual dashboard over your Alpaca **paper** account. It monitors the
account + positions in real time and can act on them (flatten, run a strategy
tick, place manual buy/sell orders).

## Launch

```bash
cd /Users/peter/Desktop/Old_Projects/GitHub/Alpaca_Paper_Trader
source venv/bin/activate
python3 tui.py
```

Needs a real terminal (full-screen app). Reads creds from `.env`.

## Screen layout (top → bottom)

```
┌──────────────────────────────────────────────────────────────┐
│ Alpaca Paper Trader — Cockpit                     🕒 14:23:01  │  ← Header (title + clock)
├──────────────────────────────────────────────────────────────┤
│ Equity $103,420  Cash $12,300  RegT(2x) $24,600  DT(4x) ...   │  ← Summary line
│   DayP&L +1,240 (+1.21%)   Exposure 1.84x                     │
├──────────────────────────────────────────────────────────────┤
│            DISARMED — safe (press 'a' to arm)                 │  ← Arm bar (green=safe / red=armed)
├──────────────────────────────────────────────────────────────┤
│ SYM    QTY    AVG     PRICE   P&L $     P&L %                 │  ← Holdings table
│ NVDA   50    120.00  131.20   +560      +9.3%   ◄ cursor row  │    (sorted by P&L, green/red)
│ AAPL   30    190.00  188.10   -57       -1.0%                 │
│ ...                                                           │
├──────────────────────────────────────────────────────────────┤
│ booted DISARMED — press 'a' to enable live actions           │  ← Event log (last ~12 lines)
│ refreshed 7 positions @ 14:23:01                             │
├──────────────────────────────────────────────────────────────┤
│ r refresh  d dry-run  a arm/disarm  q quit • f flatten ...   │  ← Footer (wraps when narrow)
└──────────────────────────────────────────────────────────────┘
```

Five regions:

1. **Header** — app title and a live clock.
2. **Summary line** — Equity, Cash, RegT buying power (2x), Day-trading buying
   power (4x), Day P&L (green/red), and Exposure (leverage = long market value
   / equity). A second row shows a **MARKET OPEN / MARKET CLOSED** badge. When
   closed, orders are queued by Alpaca and fill at the next open — arming while
   closed logs a reminder of this.
3. **Arm bar** — your safety indicator.
   - **Green "DISARMED — safe"** = read-only, mutating keys do nothing.
   - **Red "ARMED — live orders ENABLED"** = order keys are hot.
4. **Holdings table** — one row per position, sorted by unrealized P&L
   (winners on top). Columns: SYM, QTY, AVG entry, current PRICE, P&L $, P&L %.
   P&L cells are green/red. **Move the row cursor with ↑/↓** — the selected row
   is what `s` (sell) acts on.
5. **Event log** — scrolling status: refreshes, dry-run output, order
   confirmations, and errors.

Data auto-refreshes every **8 seconds**.

## Keys

| Key | Action | Needs ARM? | Confirm modal? |
|-----|--------|:----------:|:--------------:|
| `r` | Refresh now | — | — |
| `d` | Dry-run RS ranking (read-only preview of intraday longs) | — | — |
| `a` | Arm / Disarm toggle | — | — |
| `q` | Quit | — | — |
| `f` | **Flatten ALL** positions to cash | ✅ | ✅ |
| `t` | Live intraday momentum tick | ✅ | ✅ |
| `c` | Live Capitol Copier cycle | ✅ | ✅ |
| `b` | Manual **Buy** (prompts symbol + notional $) | ✅ | ✅ |
| `s` | **Sell ALL** of the cursor-selected row | ✅ | ✅ |
| `e` | **Rebalance to top-N** — keep best N by P&L %, sell the rest, redeploy cash | ✅ | ✅ |

## Two safety gates on every order

Every account-mutating action (F, T, C, B, S) passes **two** independent gates:

1. **ARM switch** — the app boots **DISARMED**. Order keys are inert until you
   press `a` (arm bar turns red). If you press an order key while disarmed, the
   log just says `DISARMED — press 'a' first`.
2. **Confirm modal** — even when armed, each action pops a Yes/No dialog showing
   exactly what it will do. `y`/Enter = Yes, `n`/Esc = No.

The Buy flow has an extra step: `b` first opens a form (symbol + notional USD),
*then* shows the confirm modal.

## Typical session

1. Launch → it boots DISARMED, positions load.
2. Press `d` to preview the intraday RS ranking (safe, no orders).
3. Watch the summary + holdings update every 8s.
4. To act: press `a` (bar goes red) → press the action key → confirm Yes.
5. Press `a` again to disarm when done, or `q` to quit.

> Note: per project status, the live strategies are gated on sign-off and the
> gateway was reported down — verify `ALPACA_BASE_URL` points at
> `https://paper-api.alpaca.markets` before arming.
