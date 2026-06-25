# Alpaca Paper Trader

An automated paper trading system that copies U.S. congressional stock disclosures into an [Alpaca](https://alpaca.markets) paper account, runs an independent TSLA ladder strategy, and delivers a daily Telegram report with full portfolio analytics.

---

## Overview

Congress members are required by the STOCK Act to disclose personal stock trades within 45 days. This system:

1. **Monitors** public trade disclosures from a curated pool of high-performing politicians via [Capitol Trades](https://www.capitoltrades.com)
2. **Copies** qualifying buys and sells into an Alpaca paper trading account in near-real-time
3. **Scores and rotates** politicians weekly using backtested alpha, win rate, disclosure speed, and sector diversity
4. **Runs an independent** TSLA trailing-stop + buy-the-dip ladder strategy alongside the copy trades
5. **Reports** a full portfolio snapshot to Telegram every weekday at 4 PM ET

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
2. **Summary line** — Equity, Cash, RegT (2x) & Daytrading (4x) buying power, Day P&L (green/red), Exposure (leverage).
3. **Arm bar** — safety indicator: **green = DISARMED/safe**, **red = ARMED/live**.
4. **Holdings table** — one row per position, **sorted by P&L** (winners on top): SYM, QTY, AVG, PRICE, P&L $, P&L %. These are your **live paper positions** (pulled from `GET /positions`), not a watchlist. Move the row cursor with ↑/↓ — that row is what `S` sells.
5. **Event log** — scrolling refreshes, dry-run output, order confirmations, errors.

Data auto-refreshes every **8 seconds** (or `r` to force it).

### Keys

| Key | Action | Needs ARM? | Confirm modal? |
|-----|--------|:----------:|:--------------:|
| `r` | Refresh now | — | — |
| `d` | Dry-run RS ranking (read-only preview of intraday longs) | — | — |
| `a` | Arm / Disarm toggle | — | — |
| `q` | Quit | — | — |
| `F` | **Flatten ALL** positions to cash | ✅ | ✅ |
| `T` | Live intraday momentum tick | ✅ | ✅ |
| `C` | Live Capitol Copier cycle | ✅ | ✅ |
| `B` | Manual **Buy** (prompts symbol + notional $) | ✅ | ✅ |
| `S` | **Sell ALL** of the cursor-selected row | ✅ | ✅ |

### Arm / Disarm safety

The cockpit boots **DISARMED** (read-only). Every account-mutating key (`F` `T` `C` `B` `S`) passes **two** independent gates:

1. **ARM switch** — order keys are inert until you press `a` (arm bar turns red). Pressing one while disarmed just logs `DISARMED — press 'a' first`.
2. **Confirm modal** — even when armed, each action pops a Yes/No dialog showing exactly what it will do (`y`/Enter = Yes, `n`/Esc = No).

So nothing executes until you've deliberately armed *and* confirmed. Press `a` again to disarm.

See [`TUI_GUIDE.md`](TUI_GUIDE.md) for the full ASCII layout diagram and a session walkthrough.

---

## Scripts

| Script | Role |
|--------|------|
| `tui.py` | **Interactive trading cockpit** — live Textual dashboard; monitors account/positions and can flatten, run strategy cycles, or place manual buy/sell orders (arm + confirm gated). |
| `hermes_report.py` | **Main daily report** — fetches all Alpaca data, builds 4-part Telegram report + charts. Runs at 4 PM ET via launchd. |
| `capitol_copier.py` | Scrapes Capitol Trades, copies qualifying politician buys/sells to Alpaca |
| `intraday_momentum.py` | Intraday momentum / relative-strength day-trading strategy (4x DT buying power, flat-to-cash daily) |
| `trader.py` | Base Alpaca API wrapper (`get_account`, `get_positions`, `place_order`, `get_orders`) |
| `tsla_strategy.py` | TSLA trailing-stop + ladder strategy execution |
| `pool_manager.py` | Manages pool membership, trade sizing, consensus detection |
| `politician_vetter.py` | Scores politicians, selects top 5 for pool |
| `backtest_engine.py` | Backtests politician trades vs SPY across 5/10/15/30/60-day hold windows |
| `politician_history.py` | Tracks per-politician pool history and probation weeks |
| `performance_tracker.py` | Syncs filled Alpaca orders, pairs buy/sell trades, computes P&L vs SPY benchmark |
| `sentiment_check.py` | Fetches news headlines per ticker, scores sentiment via local LLM (Ollama) |
| `daily_briefing.py` | Morning AI briefing — reads all state files, feeds to LLM, pushes to Telegram |
| `event_watcher.py` | Watches state files for changes every 5 min, fires Telegram alerts on new trades |
| `sunday_review.py` | Weekly automated review — adjusts strategy config, rebalances pool weights |
| `portfolio_report.py` | Full terminal snapshot report (run manually anytime) |
| `hermes_client.py` | HTTP client for local Ollama LLM (gemma4:26b at localhost:11434) |
| `telegram_notifier.py` | Telegram Bot push notification helper |
| `cron_wrapper.sh` | launchd shell wrapper — self-gates to 4 PM ET weekdays only |

---

## State Files

| File | Contents |
|------|----------|
| `strategy_config.json` | Live strategy parameters (stop %, ladder levels, pool budget) |
| `pool_state.json` | Current pool — scores, weights, probation flags |
| `.copied_trades.json` | Dedup log — last check timestamp, buys/sells per politician |
| `.tsla_state.json` | TSLA trailing stop state (highest stop price, trailing active flag) |
| `performance_log.json` | Closed trade history with P&L, strategy tag, SPY benchmark |
| `.sentiment_cache.json` | Cached LLM sentiment scores by ticker (TTL-based) |
| `politician_universe.json` | Full universe of tracked politicians with trade counts |
| `politician_history.json` | Per-politician pool history and probation record |
| `backtest_results.json` | Cached backtest results per politician |
| `review_log.json` | Weekly Sunday review history and config change log |
| `vetting_log.json` | Pool vetting run history |

---

## Daily Telegram Report

Delivered every weekday at **4 PM ET** via `cron_wrapper.sh` → `hermes_report.py`.

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

---

## Setup

### Requirements

```bash
pip install -r requirements.txt
```

`requirements.txt` also needs `matplotlib` for charts — add it if missing:

```bash
pip install requests python-dotenv matplotlib
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

# Full terminal portfolio snapshot
python3 portfolio_report.py

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

The `cron_wrapper.sh` is designed to be triggered by a launchd plist that fires hourly. It self-gates: only runs `hermes_report.py` when the hour is 4 PM ET on a weekday.

```
launchd (hourly)
  └─► cron_wrapper.sh
        └─ gate: weekday AND 16:00 ET?
            YES → python3 hermes_report.py → Telegram
            NO  → skip (log tick only)
```

---

## Project Structure

```
Alpaca_Paper_Trader/
├── tui.py                    # interactive trading cockpit (Textual)
├── hermes_report.py          # daily report entry point
├── capitol_copier.py         # smart money copy engine
├── intraday_momentum.py      # intraday RS day-trading strategy (4x)
├── trader.py                 # Alpaca API base wrapper
├── tsla_strategy.py          # TSLA ladder strategy
├── pool_manager.py           # pool membership + trade sizing
├── politician_vetter.py      # weekly pool scoring
├── backtest_engine.py        # historical backtesting
├── politician_history.py     # pool history tracker
├── performance_tracker.py    # P&L sync + benchmarking
├── sentiment_check.py        # LLM sentiment scoring
├── daily_briefing.py         # morning AI briefing
├── event_watcher.py          # real-time state watcher
├── sunday_review.py          # weekly auto-review
├── portfolio_report.py       # manual terminal report
├── hermes_client.py          # Ollama LLM client
├── telegram_notifier.py      # Telegram push helper
├── cron_wrapper.sh           # launchd gate script
├── strategy_config.json      # live strategy parameters
├── pool_state.json           # active politician pool
├── performance_log.json      # closed trade history
├── requirements.txt
├── .gitignore
├── .env                      # (git-ignored — add your own)
└── reports/
    └── alpaca_YYYYMMDD_HHMMSS.md   # archived daily reports
```

---

## Disclaimer

This project uses publicly available congressional trade disclosure data for educational and research purposes. It trades only in a paper (simulated) account. Not financial advice.
