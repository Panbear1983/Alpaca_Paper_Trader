# Alpaca Paper Trader

**Repository profile:** A supervised Alpaca paper-trading cockpit with congressional trade-copy automation, live Textual controls, multi-timeframe account charts, Telegram reporting, and archived report artifacts.

Alpaca Paper Trader is a paper-account research system that copies U.S. congressional stock disclosures into an [Alpaca](https://alpaca.markets) account, scores and rotates the politician pool, supports supervised manual actions from a terminal cockpit, and preserves daily portfolio reports for review.

---

## Overview

Congress members are required by the STOCK Act to disclose personal stock trades within 45 days. This system:

1. **Monitors** public trade disclosures from a curated pool of high-performing politicians via [Capitol Trades](https://www.capitoltrades.com)
2. **Copies** qualifying buys and sells into an Alpaca paper trading account in near-real-time
3. **Scores and rotates** politicians weekly using backtested alpha, win rate, disclosure speed, and sector diversity
4. **Runs an independent** TSLA trailing-stop + buy-the-dip ladder strategy alongside the copy trades
5. **Supervises** the account from a Textual TUI with arm/confirm safety gates, manual buy/sell/rebalance flows, and selectable 1D-1Y charts
6. **Reports and archives** full portfolio snapshots as Telegram messages, chart images, and Markdown report artifacts

This is a paper trading (simulated money) research project — not financial advice.

---

## System Architecture

```
Capitol Trades (web scrape)          Alpaca Trading API
        |                                    |
        v                                    v
capitol_copier.py              hermes_report.py (daily 4PM ET)
        |                          |    |    |
        |                    /account /positions /orders
        v                          |
  pool_state.json            build_report()
  .copied_trades.json              |
  strategy_config.json             v
        |                   4× Telegram messages
        v                   + 2× chart photos (PNG)
politician_vetter.py
(weekly scoring + pool selection)
        |
backtest_engine.py
(90-day backtests vs SPY)
```

---

## Strategies

### Capitol Copier (Smart Money Pool)

Tracks a pool of up to 5 congress members selected by a weekly vetting engine. Each politician is scored on:

| Factor | Weight |
|--------|--------|
| Realized return alpha vs SPY | 30% |
| Win rate | 25% |
| Disclosure speed (days to file) | 15% |
| Sector diversification | 10% |
| Activity consistency | 10% |
| Sample size confidence | 10% |

Trade size scales by pool rank (Rank 1 gets 40% of daily budget). A **consensus boost** doubles position size when 2+ pool members buy the same ticker within 14 days.

**Active Pool** (vetted 2026-05-27):

| Rank | Politician | Party | Win Rate | Alpha | Status |
|------|-----------|-------|----------|-------|--------|
| 1 | Richard Blumenthal (CT) | DEM | 79% | +12.2% | Full |
| 2 | Josh Gottheimer (NJ) | DEM | 59% | -2.3% | Full |
| 3 | Michael McCaul (TX) | REP | 86% | +1.6% | Full |
| 4 | Maria Elvira Salazar (FL) | REP | 50% | +0.4% | Probation |
| 5 | David Taylor (OH) | REP | 57% | -1.2% | Probation |

Pool members on **probation** for 2 weeks if win rate drops below 30%. Auto-removed if they remain below threshold.

### TSLA Ladder Strategy

An independent position that buys additional TSLA shares at pre-set price drops from entry, then exits with a trailing stop.

- Entry: $422.27 (10 shares)
- Stop loss: 10% below entry
- Trailing stop: activates after +10% gain, trails 5% below peak
- Buy-the-dip ladder: 5 levels (L1–L5) at 15%–40% below entry

---

## Interactive TUI Cockpit

`tui.py` is a live [Textual](https://textual.textualize.io) dashboard over the paper account. It **monitors** the account + positions in real time and can **act** on them — flatten everything, run a strategy cycle on demand, or place manual buy/sell orders. Unlike the background scripts, it's a hands-on cockpit you supervise and steer.

> **⚠️ Paper vs Live — the single switch.** Every action (TUI or script) trades wherever `ALPACA_BASE_URL` points. The shipped value is the **paper** endpoint (`https://paper-api.alpaca.markets/v2`) — simulated money, no real risk. Changing it to `https://api.alpaca.markets` makes **all** orders real-money live. There is no other guard in code; this URL is the only thing standing between paper and live.

### Launch

```bash
source venv/bin/activate        # or: pip install -r requirements.txt
python3 tui.py                  # needs a real terminal (full-screen app)
```

### Screen layout (top → bottom)

1. **Header** — app title + live clock.
2. **Summary line** — Equity, Cash, RegT (2x) & Daytrading (4x) buying power, Day P&L (green/red), Exposure (leverage). Second row shows a **MARKET OPEN / MARKET CLOSED** badge; when closed it notes that orders queue to the next open. Orders are `time_in_force=day` market orders, so anything placed while the market is closed is **queued by Alpaca and fills at the next open** — and arming while closed logs a reminder of this.
3. **Arm bar** — safety indicator: **green = DISARMED/safe**, **red = ARMED/live**.
4. **Holdings table** — one row per position, **sorted by P&L** (winners on top): SYM, QTY, AVG, PRICE, P&L $, P&L %. These are your **live paper positions** (pulled from `GET /positions`), not a watchlist. Move the row cursor with ↑/↓ — that row is what `s` sells.
5. **Bottom half (stacked, full width):**
   - **Top — candlestick chart:** fetched from Alpaca historical bars for the selected holding, or synthetic equity bars for the whole portfolio. It **follows the selected holding** (move the cursor ↑/↓), press **`o`** to chart the **whole portfolio (equity)**, and press **`w`** to cycle **1D → 1W → 1M → 3M → 6M → 1Y**. **Click a candle** to print its time + price in the log (approximate — terminals don't expose true plot hit-testing).
   - **Below — event log:** scrolling refreshes, dry-run output, order confirmations, errors.

The holdings table takes the **top half**; the full-width candlestick chart and log are stacked in the **bottom half**.

Data auto-refreshes every **8 seconds** (or `r` to force it).

### Keys

| Key | Action | Needs ARM? | Confirm modal? |
|-----|--------|:----------:|:--------------:|
| `r` | Refresh now | — | — |
| `d` | Dry-run RS ranking (read-only preview of intraday longs) | — | — |
| `p` | **Push the full portfolio report** to Telegram now (same format as the daily report) | — | — |
| `o` | Toggle the chart between the **selected holding** and the **whole portfolio** (equity) | — | — |
| `w` | Cycle the chart timeframe: **1D → 1W → 1M → 3M → 6M → 1Y** | — | — |
| `g` | **Edit the scheduled** auto-report — opens a modal for on/off, time (HH:MM ET), weekdays-only, and channel | — | — |
| `m` | **Edit Telegram channels** — set the default channel or add a channel (config only; chat-ID/token values stay in `.env`) | — | — |
| `a` | Arm / Disarm toggle | — | — |
| `q` | Quit | — | — |
| `f` | **Flatten ALL** positions to cash | ✅ | ✅ |
| `t` | Live intraday momentum tick | ✅ | ✅ |
| `c` | Live Capitol Copier cycle | ✅ | ✅ |
| `b` | Manual **Buy** (symbol + notional $) — live "cash after" readout as you type | ✅ | ✅ |
| `s` | **Sell** the cursor-selected row — `all` or a $ amount (partial); live "cash after / position left" readout | ✅ | ✅ |
| `e` | **Rebalance to top-N** — keep the best N by P&L %, sell the rest, redeploy proceeds; then pick ONE: **deploy idle cash** (buy, ~1x) or **withdraw / raise cash** (trim holdings to a $ target). Each field takes a $ amount or `all`, with a live readout | ✅ | ✅ |

### Arm / Disarm safety

The cockpit boots **DISARMED** (read-only). Every account-mutating key (`f` `t` `c` `b` `s`) passes **two** independent gates:

1. **ARM switch** — order keys are inert until you press `a` (arm bar turns red). Pressing one while disarmed just logs `DISARMED — press 'a' first`.
2. **Confirm modal** — even when armed, each action pops a Yes/No dialog showing exactly what it will do (`y`/Enter = Yes, `n`/Esc = No).

So nothing executes until you've deliberately armed *and* confirmed. Press `a` again to disarm.

### Telegram notifications

When you execute an action in the TUI (buy/sell/flatten/rebalance/tick/capitol),
it sends a one-line **"submitted"** alert to Telegram via the existing
`telegram_notifier.py` (same **@Panbear_Hermes_bot** the daily report uses). Bulk
actions (flatten/rebalance) send a single batched message, and alerts fired while
the market is closed note that the order is **queued to the next open**. Market
open↔closed transitions also ping once. This is **send-only** — it does not poll,
so it never conflicts with other consumers of the bot. Mute it via
`strategy_config.json` → `"tui": { "telegram_notify": false }`. The daily report
(`hermes_report.py`) is unaffected.

See [`TUI_GUIDE.md`](TUI_GUIDE.md) for the full ASCII layout diagram and a session walkthrough.

---

## Scripts

| Script | Role |
|--------|------|
| `tui.py` | **Interactive trading cockpit** — live Textual dashboard; monitors account/positions, charts holdings or portfolio equity across 1D-1Y, and can flatten, rebalance, run strategy cycles, or place manual buy/sell orders behind arm + confirm gates. |
| `hermes_report.py` | **Daily report and chart data hub** — builds the Telegram report, writes Markdown report artifacts, generates charts, and exposes account/market helpers used by the TUI. |
| `report_scheduler.py` | Config-driven report trigger; launchd fires a heartbeat, then this script gates on `report_schedule` in `strategy_config.json` and dedupes to one report/day. |
| `capitol_copier.py` | Scrapes Capitol Trades and copies qualifying politician buys/sells to Alpaca. |
| `intraday_momentum.py` | Intraday momentum / relative-strength day-trading strategy using day-trading buying power. |
| `rebalance_top_n.py` | Rebalance helper used by the TUI to keep top performers, raise cash, or deploy idle cash. |
| `config_io.py` | Shared config read/write helper for TUI schedule and Telegram channel settings. |
| `pool_manager.py` | Manages pool membership, trade sizing, consensus detection, and exposure limits. |
| `politician_vetter.py` | Scores politicians and selects the active pool. |
| `politician_history.py` | Tracks per-politician pool history and probation weeks. |
| `backtest_engine.py` | Backtests politician trades vs SPY across multiple hold windows. |
| `performance_tracker.py` | Syncs filled Alpaca orders, pairs buy/sell trades, and computes realized P&L vs SPY. |
| `sentiment_check.py` | Fetches ticker news headlines and scores sentiment. |
| `openrouter_analyst.py` | Optional analyst-summary client for report commentary. |
| `event_watcher.py` | Watches state files for changes and fires Telegram alerts on new trades or status changes. |
| `sunday_review.py` | Weekly automated review and strategy adjustment workflow. |
| `status.py` | Lightweight account/status inspection helper. |
| `hermes_client.py` | HTTP client for the local LLM endpoint. |
| `telegram_notifier.py` | Telegram Bot push notification helper. |
| `propose_change.py` / `apply_change.py` | Proposal/apply workflow helpers for staged autonomous edits. |
| `cron_wrapper.sh` | Legacy launchd shell wrapper that runs the report directly at 4 PM ET; the current plist in `launchd/` runs `report_scheduler.py` directly. |

---

## State Files

| File | Contents |
|------|----------|
| `strategy_config.json` | Live strategy parameters (stop %, ladder levels, pool budget) |
| `pool_state.json` | Current pool — scores, weights, probation flags |
| `.copied_trades.json` | Dedup log — last check timestamp, buys/sells per politician |
| `.event_watcher_state.json` | Event watcher dedupe state for filled-order and pool-change alerts |
| `.report_schedule_state.json` | Runtime dedupe state for the config-driven report scheduler (git-ignored) |
| `performance_log.json` | Closed trade history with P&L, strategy tag, SPY benchmark |
| `.sentiment_cache.json` | Cached LLM sentiment scores by ticker (TTL-based) |
| `politician_universe.json` | Full universe of tracked politicians with trade counts |
| `politician_history.json` | Per-politician pool history and probation record |
| `backtest_results.json` | Cached backtest results per politician |
| `review_log.json` | Weekly Sunday review history and config change log |
| `vetting_log.json` | Pool vetting run history |

---

## Daily Telegram Report

Delivered on the configured schedule via `launchd/com.alpacapapertrader.report_scheduler.plist` → `report_scheduler.py` → `hermes_report.py`, or manually from the TUI with `p`.

**4 messages + 2 chart photos:**

```
Message 1 — Account Summary
  Equity / Cash / Day P&L / SPY alpha
  Portfolio totals: cost basis / market value / unrealized P&L

Message 2 — Holdings  (39 positions, sorted best → worst)
  SYM | RET% | P&L$
  TOTAL row: sum unrealized / sum cost basis

Message 3 — Today's Purchases
  Time ET | Symbol | Qty | Fill price | Cost

Message 4 — Watchlist
  SPY / QQQ / TSLA / NVDA / AAPL mid prices

Photo 1 — 30-day equity curve
Photo 2 — Allocation pie by market value
```

The same run also writes local artifacts under `reports/`.

---

## Report Artifacts

Report runs create a reviewable artifact trail:

| Pattern | Git policy | Contents |
|---------|------------|----------|
| `reports/alpaca_YYYYMMDD_HHMMSS.md` | Tracked | Markdown copy of the portfolio report sent to Telegram. |
| `reports/alpaca_equity_YYYYMMDD_HHMMSS.png` | Ignored | Generated equity-curve chart image. |
| `reports/alpaca_alloc_YYYYMMDD_HHMMSS.png` | Ignored | Generated allocation pie chart image. |
| `reports/*.log` | Ignored | launchd / scheduler runtime logs. |

Markdown reports are intentionally kept in the repository so account snapshots and analyst commentary can be reviewed in pull requests. PNGs and logs are derived runtime artifacts and can be regenerated by running `python3 hermes_report.py` or pressing `p` in the TUI.

---

## Setup

### Requirements

```bash
pip install -r requirements.txt
```

### Environment Variables

Create `.env` in the repo root (never commit this file):

```env
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_HOME_CHANNEL=your_chat_id
WATCHLIST=SPY,QQQ,TSLA,NVDA,AAPL
```

Get paper trading keys from: https://app.alpaca.markets → Paper Trading → API Keys

### Run Manually

```bash
# Full report — generate + push to Telegram
python3 hermes_report.py

# Dry run — generate only, print to stdout, skip Telegram
python3 hermes_report.py --dry-run

# Read-only terminal account/status snapshot
python3 status.py

# Copy latest politician trades now
python3 capitol_copier.py

# Re-score and update politician pool
python3 politician_vetter.py

# Backtest a specific politician
python3 backtest_engine.py --politician B001277

# Weekly review (normally runs Sunday automatically)
python3 sunday_review.py
```

### launchd Automation (macOS)

The current launchd job lives at `launchd/com.alpacapapertrader.report_scheduler.plist`. It fires every 10 minutes, runs `report_scheduler.py`, and lets the scheduler self-gate from `strategy_config.json` (`report_schedule`) so the report time can be edited from config or the TUI.

```
launchd (10-min heartbeat)
  └─► report_scheduler.py
        └─ gate: enabled, weekday, configured ET time window, not already sent today?
            YES → hermes_report.run_report(push=True) → Telegram + reports/
            NO  → skip (log tick only)
```

---

## Project Structure

```
Alpaca_Paper_Trader/
├── tui.py                    # interactive trading cockpit (Textual)
├── hermes_report.py          # report builder + chart/account helpers
├── report_scheduler.py       # config-driven daily report trigger
├── capitol_copier.py         # smart money copy engine
├── intraday_momentum.py      # intraday RS day-trading strategy (4x)
├── rebalance_top_n.py        # TUI rebalance helper
├── config_io.py              # shared config persistence
├── pool_manager.py           # pool membership + trade sizing
├── politician_vetter.py      # weekly pool scoring
├── politician_history.py     # pool history tracker
├── backtest_engine.py        # historical backtesting
├── performance_tracker.py    # P&L sync + benchmarking
├── sentiment_check.py        # LLM sentiment scoring
├── openrouter_analyst.py     # optional analyst commentary
├── event_watcher.py          # real-time state watcher
├── sunday_review.py          # weekly auto-review
├── status.py                 # status helper
├── hermes_client.py          # Ollama LLM client
├── telegram_notifier.py      # Telegram push helper
├── propose_change.py         # staged change proposal helper
├── apply_change.py           # staged change apply helper
├── cron_wrapper.sh           # legacy launchd gate script
├── strategy_config.json      # live strategy parameters
├── pool_state.json           # active politician pool
├── performance_log.json      # closed trade history
├── requirements.txt
├── .gitignore
├── .env                      # (git-ignored — add your own)
└── reports/
    ├── alpaca_YYYYMMDD_HHMMSS.md       # tracked Markdown report artifacts
    ├── alpaca_equity_YYYYMMDD_HHMMSS.png  # ignored generated chart images
    └── alpaca_alloc_YYYYMMDD_HHMMSS.png   # ignored generated chart images
```

---

## Disclaimer

This project uses publicly available congressional trade disclosure data for educational and research purposes. It trades only in a paper (simulated) account. Not financial advice.
