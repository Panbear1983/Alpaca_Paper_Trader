# Alpaca Paper Trader

**Repository profile:** A supervised Alpaca paper-trading cockpit with congressional trade-copy automation, live Textual controls, multi-timeframe account charts, Telegram reporting, and archived report artifacts.

Alpaca Paper Trader is a paper-account research system that copies U.S. congressional stock disclosures into an [Alpaca](https://alpaca.markets) account, scores and rotates the politician pool, supports supervised manual actions from a terminal cockpit, and preserves daily portfolio reports for review.

---

## Chapter II — Strategy Concept (Rules of Engagement)

This chapter is a trading primer as much as a spec. Each section below names a
real technique from the wider world of trading and investing, explains *what it
is and why traders use it* in general, and then shows how this specific cockpit
applies it. If you're new to trading, read this chapter as a glossary of
professional risk-management ideas; if you already trade, it's the map from
"industry concept" to "the exact knob that controls it" — see
[Strategy Config Editor](#strategy-config-editor-n) for the latter.

### Copy trading & "smart money" signals

**The concept:** Copy trading means deliberately mirroring the trades of
participants believed to have an informational or analytical edge, instead of
researching every idea yourself. It works only as well as your source does —
which is why professional allocators don't treat every signal equally. Two
refinements matter:
- **Conviction-weighted sizing** — put more capital behind sources with a
  stronger track record, rather than spreading bets evenly.
- **Consensus** — when multiple *independent* sources converge on the same
  idea without coordinating, that agreement is treated as a stronger signal
  than any one of them alone, because it's less likely to be one person's
  mistake or blind spot.
- **Signal decay** — a piece of information is worth less the longer it's been
  public, since the market has had more time to already price it in.
- **Manager rotation** — allocators continuously score their sources and cut
  the ones who stop performing, rather than following anyone forever on faith.

**How this cockpit applies it:** the Capitol Copier mirrors stock trades that
U.S. members of Congress are legally required to disclose (the STOCK Act gives
them 45 days), across a pool of up to 5 tracked politicians. Capital is split
by pool *rank*, not evenly (conviction-weighted sizing). A buy gets sized up —
up to 5x — when 2+ pool members disclose the same ticker within 14 days
(consensus). Disclosures older than a max-age cutoff are skipped outright
(signal decay). The pool is re-scored every week on alpha vs. SPY, win rate,
disclosure speed, sector spread, and sample size — a member whose win rate
drops below 30% goes on 2-week probation and is dropped if it doesn't recover
(manager rotation). A sector whitelist and an optional bearish-sentiment veto
sit on top as independent risk filters, not signal sources.

### Stop-loss, trailing stops, and pyramiding

**The concept:** these are the core tools of position-level risk management —
what happens to a trade *after* you're in it.
- A **stop-loss** is a predetermined price at which a losing position is
  closed automatically, so a bad trade has a known, bounded cost instead of an
  open-ended one.
- A **trailing stop** is a stop that moves in your favor as a position gains,
  locking in profit while still leaving room for the trade to keep running —
  the alternative to picking a fixed profit target and hoping you picked
  correctly.
- **Pyramiding** is adding to a position that's already proving you right,
  increasing size into strength. It's the deliberate opposite of "averaging
  down" (adding to a position that's proving you wrong), which is one of the
  more common ways undisciplined traders turn a small loss into a large one.
- A **max-holdings cap** is a portfolio-level rule limiting how many
  simultaneous bets you run, so the book stays a set of positions you can
  actually reason about rather than an unmanaged sprawl.

**How this cockpit applies it:** the Dynamic Exit Engine runs all four of
these, on every held position, in every wallet, on a fixed check cadence
during market hours. The pattern was proven on a single name before being
generalized — the founding case study bought TSLA at $422.27 with a 10% stop,
a trailing stop that only arms after a +10% gain (then trails 5% off the
peak), and a 5-level buy-the-dip ladder at 15%–40% below entry. The exit
engine now runs that same stop/trail/pyramid logic system-wide, and a
cap-tail rule sells the smallest positions if the book exceeds its max-holdings
limit.

### Momentum, relative strength, and trend (regime) filters

**The concept:** **Momentum investing** rests on a well-documented market
tendency: assets that have recently *outperformed* tend to keep outperforming
over medium-term horizons, more often than pure chance would predict. This is
the opposite instinct from bargain-hunting — you're buying strength, not
buying because something looks cheap. **Relative strength** is simply how you
measure this: ranking assets by how much better (or worse) they've done than a
benchmark. A **regime** or **trend filter** is a separate, market-wide check —
typically "is price above its own moving average?" — used to decide whether
to be participating in new buys *at all* right now, independent of any single
stock's momentum; it exists because momentum strategies tend to perform badly
when the overall market is in a downtrend, so many systems simply stand down
during one rather than trying to pick momentum winners against the tide.

**How this cockpit applies it:** the Swing Buyer ranks its tracked watchlist
by relative strength vs. SPY once a day and buys the strongest names, capped
at a max number of new entries per run. Its regime filter goes fully quiet —
zero new buys — whenever SPY is below its own moving average. It also
dip-adds to *existing winners* (a position already up a meaningful amount from
entry that's pulled back off its peak) rather than to laggards, gated by a
per-name cooldown, and it won't immediately re-buy a name the exit engine just
stopped out of — sitting out a cooldown of its own so a losing trade doesn't
get repeated on autopilot. A minimum cash reserve is always kept in hand.

### Leverage and day trading

**The concept:** **Leverage** means trading with more buying power than your
own capital — e.g., "4x leverage" lets a $1,000 move control roughly $4,000 of
exposure. It multiplies gains, but it multiplies losses by exactly the same
factor, which is why leveraged strategies are considered materially
higher-risk than unleveraged ones. **Day trading** means closing every
position by the end of the same session rather than holding overnight — a
discipline specifically meant to avoid *overnight gap risk*: a leveraged
position can't be protected by a stop-loss while the market is closed, so
professional leveraged day traders flatten before the close as standard
practice, not as an afterthought.

**How this cockpit applies it:** Intraday Momentum is a separate 4x leveraged
day-trading strategy, off by default and deliberately kept out of the shared
`n` config editor. It flattens at end of day by design. It's also never run
alongside the other autonomous engines — its flatten would liquidate the
entire swing book if it were left on unattended, so it stays a hand-edit-only,
opt-in strategy in `strategy_config.json`.

### Rebalancing

**The concept:** **Rebalancing** is periodically trimming a portfolio back
toward a target shape — keep the winners, cut the laggards, redeploy the
proceeds — rather than letting a portfolio passively drift wherever price
action takes it. It's a direct counter to one of the most common behavioral
mistakes in investing: letting losers run because selling means admitting a
mistake, while cutting winners early because a small profit feels safe to
lock in. Rebalancing forces the opposite by rule instead of by feel.

**How this cockpit applies it:** Rebalance-to-Top-N is a manual, on-demand
tool (not a scheduled engine): keep only the best N positions by P&L%, sell
the rest, then choose to either deploy the freed cash into more buying or
withdraw it to raise cash to a target.

### Pre-trade risk controls

**The concept:** professional trading desks don't let any single click send
an order. Two habits are close to universal: a **confirmation step** that
shows exactly what's about to happen before it's final (catching "fat-finger"
mistakes — a wrong size, a wrong symbol), and a **pre-trade compliance check**
that validates an order is even legal to place (tradable, correct share
rounding, market open) before it's submitted, rather than finding out from a
rejection after the fact.

**How this cockpit applies it:** every account-mutating action — manual or
autonomous — requires the cockpit to be explicitly **armed** first, and pops a
**Confirm** modal stating exactly what it's about to do. Manual buys add a
live tradability check (tradable / unknown / whole-shares-only) that blocks
anything Alpaca would reject before the order can even be submitted.

Every number behind the techniques above — the exact stop percentage, the
lookback window, the daily budget — lives in one place per wallet, that
wallet's `strategy_<slug>.json`, and each wallet runs its own independent copy
of every technique (own pool, own exits, own swing book), switched with `k`.
The techniques themselves are identical across wallets; only the numbers
differ.

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
./tui.sh                        # THE way to open the cockpit: attaches to the
                                # running instance, or starts one (tmux session
                                # "alpaca_tui"). Detach: Ctrl-b d · Quit: q
./tui.sh start|stop|restart     # lifecycle control (stop = quit + HOLD)
./tui.sh status                 # session / clients / autostart / hold state
./tui.sh autostart on|off       # always-on mode (see below)
```

`tui.py` refuses to start twice (flock guard) — a second launch prints the
running PID and how to attach. A running window also watches the code on disk
and shows a **⚠ restart banner** if features/fixes land while it's open, so a
stale window can never silently eat your orders again.

### Always-on (`./tui.sh autostart on`)

The cockpit owns the daily report autopush, so it should survive reboots. With
autostart ON, a launchd agent (`com.alpacapapertrader.tui`) starts the cockpit
detached at login and re-fires a guard every 5 minutes:

| event | result |
|---|---|
| reboot / login | cockpit starts detached automatically |
| crash / dead pane | revived within ≤5 min |
| `q` inside the app | ALSO revived within ≤5 min — `q` = "restart the app" |
| `./tui.sh stop` | HOLD set — stays down until `./tui.sh` or `./tui.sh start` |

The guard script lives at `~/.local/bin/alpaca-tui-autostart` (generated by
`autostart on`; regenerate after moving the repo). It never touches the repo
path itself — only the tmux pane does — which is what keeps launchd's TCC
sandboxing (the exit-78 killer of the old scheduler jobs) out of the picture.
State/logs: `~/.local/state/alpaca_tui/`.

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
| `p` | **Push the multi-wallet report** to Telegram now (leaderboard + per-wallet day P&L, movers, and today's buys/sells — same format as the scheduled auto-report; see [Daily Telegram Report](#daily-telegram-report--multi-wallet-wallet_reportpy)) | — | — |
| `o` | Toggle the chart between the **selected holding** and the **whole portfolio** (equity) | — | — |
| `w` | Cycle the chart timeframe: **1D → 1W → 1M → 3M → 6M → 1Y** | — | — |
| `v` | Toggle main table between **POSITIONS** and **OPEN ORDERS** view | — | — |
| `g` | **Edit the scheduled** auto-report — opens a modal for on/off, time (HH:MM ET), weekdays-only, and channel | — | — |
| `m` | **Edit Telegram channels** — set the default channel or add a channel (config only; chat-ID/token values stay in `.env`) | — | — |
| `n` | **Strategy config editor** — browse and edit the **active wallet's** 32 trading tunables (each wallet owns a standalone `strategy_<slug>.json`; the header names which file you're editing). Switch wallets with `k` to edit the other book's strategy. See [Strategy Config Editor](#strategy-config-editor-n) and `PER_WALLET_STRATEGY_PLAN.md`. | — | per-field |
| `k` | **Switch wallet** — pick which Alpaca paper account the TUI views and manually acts on. Also switches which wallet's strategy file `n` edits. Autonomous engines run per wallet, each gated by its own strategy file's master switches. Inside the picker: `e` renames a wallet, **`a` adds a NEW wallet** (paste its API key + secret — validated live against `/v2/account` before anything is saved; keys go to `.env`, a strategy file is created with every engine OFF, and the account number + equity are shown so a wrong-account keypair is caught immediately) | — | — |
| `Shift+W` | **Compare wallets** — read-only scrollable page: leaderboard ranked against the fixed `$100k` paper-account baseline, all-wallets normalized equity overlay, then each wallet's equity curve + top-down payout breakdown (every position, biggest P&L first). Windows: **1D 1W 1M 3M 6M 1Y 3Y ALL** control chart scope — press `1`-`8` to jump or `w` to cycle; `r` refetches, ↑/↓ PgUp/PgDn scroll. Trade keys are disabled while open | — | — |
| `h` | **In-app manual** — this README, rendered inside the TUI with a navigable topic index. See [In-App Manual](#in-app-manual-h) below. | — | — |
| `a` | Arm / Disarm toggle | — | — |
| `l` | **Language toggle** — cycle English ⇄ 繁體中文 live; persisted to `strategy_config.json` (`tui.language`), so the TUI boots in your last-chosen language. Modals pick it up next time they open. Manual (`h`) loads `README.zh_TW.md` when present, else this English README | — | — |
| `q` | Quit | — | — |
| `f` | **Flatten ALL** positions to cash | ✅ | ✅ |
| `t` | Live intraday momentum tick | ✅ | ✅ |
| `c` | Live Capitol Copier cycle | ✅ | ✅ |
| `b` | Manual **Buy** — symbol + $ amount, live "cash after" readout. **Rules-of-engagement check**: entering the symbol shows the asset's trading rules (✓ tradable / ✗ unknown / ⚠ whole-shares-only @ price) and the form **blocks** any input Alpaca would reject; whole-share assets auto-convert your $ into shares in the confirm | ✅ | ✅ |
| `s` | **Sell** the cursor-selected row — `all` or a $ amount (partial); live "cash after / position left" readout | ✅ | ✅ |
| `e` | **Rebalance to top-N** — keep the best N by P&L %, sell the rest, redeploy proceeds; then pick ONE: **deploy idle cash** (buy, ~1x) or **withdraw / raise cash** (trim holdings to a $ target). Each field takes a $ amount or `all`, with a live readout | ✅ | ✅ |
| `x` | **Cancel** the selected open order (only active in ORDERS view, `v`) | — | ✅ |
| `X` | **Cancel ALL** open orders (only active in ORDERS view, `v`) | — | ✅ |
| `/` | **Ticker bio search** — type any symbol below the holdings table for the company profile (FMP), plus **1M / 1Y / ALL price returns** (Alpaca split-adjusted closes; ALL reaches back as far as Alpaca's data, ~2016). In 中文 mode the description is machine-translated (cached per symbol). `Esc` returns to the holdings table | — | — |

### Arm / Disarm safety

The cockpit boots **DISARMED** (read-only). Every account-mutating key (`f` `t` `c` `b` `s`) passes **two** independent gates:

1. **ARM switch** — order keys are inert until you press `a` (arm bar turns red). Pressing one while disarmed just logs `DISARMED — press 'a' first`.
2. **Confirm modal** — even when armed, each action pops a Yes/No dialog showing exactly what it will do (`y`/Enter = Yes, `n`/Esc = No).

So nothing executes until you've deliberately armed *and* confirmed. Press `a` again to disarm.

---

## Autonomous Trading Engines

Beyond the manual TUI actions above, two engines can trade **on their own schedule** —
supervised, not hands-on. Both are driven by `trading_scheduler.py`, a 10-minute
launchd heartbeat that self-gates to US market hours and dispatches:

| Engine | Cadence | What it does | Master switch |
|---|---|---|---|
| **Exit engine** (`capitol_copier.py --manage-only`) | every `manage_every_minutes` (default 20) during market hours | Stop-loss, trailing stop, take-profit, and pyramid-add checks on every held position | `capitol_copier.autorun_enabled` |
| **Swing buyer** (`swing_buyer.py`) | once daily, `swing.swing_time_et` window | Regime-filtered relative-strength entries + dip-adds on proven winners | `swing.enabled` |
| **Disclosure copier** (`capitol_copier.py`, copy loop) | once daily, `trading_schedule.copy_time_et` window | Copies fresh politician-disclosed buys/sells from the tracked pool | `capitol_copier.autorun_enabled` |

Both master switches — and every threshold, size, and cadence these engines use — are
tunable **live from the TUI** via the `n` key (next section). Nothing here requires
restarting the TUI or the scheduler; edits apply on the engine's next tick.

**Kill switch:** flip either master switch to off (via `n`, or by hand in
`strategy_config.json`), or `launchctl unload ~/Library/LaunchAgents/com.alpacapapertrader.trading_scheduler.plist`
to stop the heartbeat entirely. Full architecture, backtest results, and the
go-live runbook are in [`TRADING_UPGRADE_REPORT.md`](TRADING_UPGRADE_REPORT.md).

---

## Strategy Config Editor (`n`)

Press `n` in the TUI to open a live browser/editor over every tunable the autonomous
engines read. It's organized as a table: **SECTION | SETTING | VALUE | RANGE** — move
the cursor with ↑/↓, press **Enter** to edit the selected row, **`r`** to reload from
disk, **Esc** to close.

### How it works

- **Read this first:** the `n` page does not represent a *different* trading
  technique — it's the control panel for the *one* technique already coded in
  `swing_buyer.py` / `capitol_copier.py`. The buy/sell *logic* ("sell at a loss
  threshold", "buy the top relative-strength names") is fixed in Python. The `n`
  page only edits the *numbers* that logic reads (which threshold, how big a buy,
  how often to check) — you cannot use it to switch strategies.
- Every edit is validated against a bounds registry (`config_fields.py`) before
  it's accepted — out-of-range values are rejected in place with an inline error.
- Saves are atomic (`config_io.update_config`) and touch **only the one field you
  changed** — safe to edit while the scheduler or `sunday_review.py` is running.
- Changes take effect on the engine's **next tick** — up to `manage_every_minutes`
  for exits, or up to a day for the swing-buyer/copier windows. No restart needed.
- Fields marked **⚠ danger** show a live consequence preview before saving — e.g.
  enabling "prune off-sector holdings" lists the exact positions that would be
  sold on the next tick, computed from your current holdings at the moment you
  confirm.
- `intraday.*` (the separate 4x leveraged day-trading strategy behind the `t` key)
  is **intentionally not editable here** — it is a distinct, higher-risk strategy
  whose end-of-day flatten would liquidate the entire swing book if left on
  alongside the autonomous engines. It stays hand-edit-only in
  `strategy_config.json`, off by default.

### Full field reference

**MASTER switches**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| Swing buyer ON/OFF | `swing.enabled` | on/off | ⚠ | Daily RS-momentum buys + dip-adds. |
| Capitol autorun ON/OFF | `capitol_copier.autorun_enabled` | on/off | ⚠ | Exit engine (20-min stops/trails/TPs/pyramids) + daily disclosure copies. |

**RISK rails**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| Max exposure (frac of equity) | `pool.max_total_exposure_pct` | 0.30–1.00 | ⚠ | Hard cap on invested market value. 1.00 = fully invested (margin edge). |
| Cash reserve floor $ | `swing.min_cash_reserve_usd` | 0–50,000 | | Swing buyer never spends below this cash cushion. |
| Per-name cap $ | `pool.max_position_usd` | 500–25,000 | | No single position may exceed this market value via buys/adds. |
| Min order size $ | `pool.min_position_usd` | 50–5,000 | | Orders smaller than this are skipped. |

**EXIT engine**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| Stop-loss (frac) | `dynamic_exits.stop_loss_pct` | 0.02–0.25 | | Sell all when unrealized loss reaches this (0.08 = -8%). |
| Trail trigger (frac) | `dynamic_exits.trail_trigger_pct` | 0.01–1.00 | | Trailing stop activates once peak gain reaches this. |
| Trail giveback (frac) | `dynamic_exits.trail_giveback_pct` | 0.01–1.00 | | After trigger, sell if price falls this far off the peak. |
| Pyramid add size (frac of orig) | `dynamic_exits.pyramid_add_frac` | 0.0–1.0 | | Each pyramid tier adds this fraction of the original position size. |
| Max holdings (exit engine) | `dynamic_exits.max_holdings` | 5–40 | ⚠ | Cap-tail: exceeding this sells the smallest positions next tick. |
| Prune off-sector holdings | `dynamic_exits.prune_off_target` | on/off | ⚠⚠ | Enabling sells EVERY position outside `target_sectors` next tick. |

**SWING buyer**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| New entry size $ | `swing.entry_size_usd` | 500–10,000 | | Notional per new RS-momentum entry. |
| Max new entries / day | `swing.max_new_positions_per_run` | 0–5 | | 0 pauses new entries while keeping dip-adds. |
| RS lookback (days) | `swing.rs_lookback_days` | 5–60 | | Relative-strength ranking window vs SPY. |
| Regime SMA (days) | `swing.regime_sma_days` | 20–200 | | No new buys while SPY closes below this moving average. |
| Dip-add size $ | `swing.dip_add_usd` | 0–5,000 | | Notional added to a proven winner on a pullback. 0 disables dip-adds. |
| Dip: min peak gain (frac) | `swing.dip_min_peak_gain` | 0.03–0.30 | | Position must have been up this much at its peak to qualify. |
| Dip: pullback off peak (frac) | `swing.dip_trigger_off_peak` | 0.02–0.15 | | ...and pulled back at least this far off that peak (while still above entry). |
| Dip cooldown (days) | `swing.dip_cooldown_days` | 1–30 | | At most one dip-add per name per this many days. |
| Stop re-buy cooldown (days) | `swing.stop_cooldown_days` | 0–30 | | Never rebuy a name the exit engine stopped out within this window. |
| Max holdings (swing buyer) | `swing.max_holdings` | 5–40 | | Swing buyer opens no new names beyond this count. |

**COPIER (politician disclosures)**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| Daily copy budget $ | `pool.daily_budget_usd` | 500–5,000 | | Base budget split by pool weights when copying disclosures. |
| Consensus boost × | `pool.consensus_boost_multiplier` | 1.0–5.0 | | Size multiplier when 2+ pool members buy the same ticker within 14 days. |
| Max disclosure age (days) | `capitol_copier.max_disclosure_lag_days` | 3–45 | | Skip disclosures older than this — stale info has no edge. |
| Sentiment veto | `capitol_copier.sentiment_veto_enabled` | on/off | | If on, a 1/5 bearish LLM sentiment blocks the buy (off = only scales size). |
| Target sectors (csv) | `capitol_copier.target_sectors` | sector list | | Whitelist for NEW copy buys (see `sectors.py` for the ticker→sector map). |

**SCHEDULER**

| Setting | Config path | Range | ⚠ | What it controls |
|---|---|---|:-:|---|
| Exit engine cadence (min) | `trading_schedule.manage_every_minutes` | 5–120 | | How often stops/trails/TPs are checked during market hours. |
| Swing buy time (ET) | `trading_schedule.swing_time_et` | HH:MM | | Daily swing-buyer window start, 24h ET. |
| Copy loop time (ET) | `trading_schedule.copy_time_et` | HH:MM | | Daily disclosure-copy window start, 24h ET. |
| Daily window width (min) | `trading_schedule.window_minutes` | 10–60 | | Width of the swing/copy fire windows. |
| Weekdays only | `trading_schedule.weekdays_only` | on/off | | Skip Saturday/Sunday ticks entirely. |

The registry above is the literal source of truth read by the TUI — see
`config_fields.py` if you want the field definitions in code form.

### Field-by-field explanation

The tables above are the quick-lookup version — this is the same list with every
row actually explained, in the same SECTION groupings the `n` screen itself uses
(MASTER / RISK / EXITS / SWING / COPIER / SCHED). The screen's header line names
which wallet and file you're editing and when each engine last actually fired for
it (read from `.trading_schedule_state.json`); "intraday: LOCKED" is just a
reminder that the separate leveraged day-trading strategy from Chapter II
("Leverage and day trading") is intentionally not editable from this screen at
all.

**MASTER**
- **Swing buyer ON/OFF** — the master kill switch for the whole Swing Buyer
  engine (momentum entries + dip-adds). Flipping it off doesn't touch positions
  it already opened — it only stops new entries and new dip-adds from its next
  daily run onward.
- **Capitol autorun ON/OFF** — the master switch for *both* the daily disclosure
  copier and the exit engine's automated stop/trail/take-profit/pyramid checks;
  they share one switch because `capitol_copier.py --manage-only` is the code
  that runs the exit engine. Turning this off stops new copy buys **and** all
  automated position management in the same move — positions are then only
  protected by whatever manual attention you give them in the TUI.

**RISK**
- **Max exposure (frac of equity)** — the hard ceiling on how much of the
  account can be invested at once. At `1` (this wallet), there's no cash cushion
  at all reserved by this rule — every dollar of equity can be deployed, an
  aggressive setting fitting a "High Risk" book. Lower it to force the engines
  to always sit on some cash regardless of how attractive new candidates look.
- **Cash reserve floor $** — a dollar floor specifically for the Swing Buyer:
  it will never spend the account below this balance, no matter how many good
  entries it finds. Works alongside the exposure fraction above — whichever
  constraint binds first wins.
- **Per-name cap $** — the most market value any single position can reach
  through buys or pyramid adds. A concentration limit, so one winning name
  can't pyramid its way into an outsized share of the book.
- **Min order size $** — orders smaller than this are skipped entirely, since
  very small orders mostly generate fees and noise relative to their size, not
  meaningful position-building.

**EXITS**
- **Stop-loss (frac)** — the hard downside limit from Chapter II: at `0.08`,
  any position down 8% from cost is sold in full on the next tick, no
  exceptions.
- **Trail trigger (frac)** — how far a position must be *up* before its
  trailing stop even arms. At `0.5`, a position needs to be up 50% before the
  trailing-stop machinery switches on — below that, only the stop-loss above is
  protecting it.
- **Trail giveback (frac)** — once armed, how far price can fall from its peak
  before the trailing stop sells. At `0.25`, a position that peaked at +80%
  would sell once it gave back a quarter of that gain. Trail trigger and trail
  giveback always work as a pair — see Chapter II ("Stop-loss, trailing stops,
  and pyramiding").
- **Pyramid add size (frac of orig)** — each pyramid tier adds this fraction of
  the position's *original* entry size. At `0.5`, every add is half the size of
  the first buy — this is what lets a winner compound instead of topping out at
  its first purchase.
- **Max holdings (exit engine)** — a portfolio-wide cap-tail rule: exceed this
  count and the *smallest* positions (by size, not performance) are sold next
  tick to bring the book back down, so it never sprawls past what's trackable.
- **Prune off-sector holdings** — the most aggressive switch on this screen
  (double-warning for a reason): when ON, **every** position outside the
  COPIER section's target-sector whitelist is sold on the very next tick,
  regardless of P&L. A portfolio-realignment tool, not a routine setting —
  off here.

**SWING**
- **New entry size $** — the notional size of each brand-new relative-strength
  entry.
- **Max new entries / day** — a hard cap on how many new names can open in one
  day's run, independent of how many strong candidates are found — this
  throttles how fast the book *grows*, separately from what it buys.
- **RS lookback (days)** — the window used to rank candidates by relative
  strength vs. SPY. Shorter reacts faster to fresh momentum shifts; longer
  smooths out noise but responds slower.
- **Regime SMA (days)** — the moving-average length behind the regime filter;
  SPY closing below it shuts off all new swing buys. A shorter SMA (`50` here)
  reacts to shallower pullbacks than a longer one (e.g. 200) would.
- **Dip-add size $** — the size of a further buy into an *existing* winner
  that's pulled back, as opposed to a brand-new entry.
- **Dip: min peak gain (frac)** — how far a position must have run up from
  entry, at its peak, before it even qualifies for a dip-add — this keeps
  dip-adds targeted at names that have already proven themselves.
- **Dip: pullback off peak (frac)** — how far off that peak it must have
  pulled back, while still staying above entry, before the dip-add actually
  fires. This field and the one above it define the "buy the dip on a proven
  winner" window together — not too early, not too late.
- **Dip cooldown (days)** — the minimum gap between dip-adds on the same name,
  so the engine can't keep adding every time a position wiggles.
- **Stop re-buy cooldown (days)** — after the exit engine stops a position out,
  how long the swing buyer must wait before it's allowed to re-buy that same
  name — the discipline against immediately repeating a trade that just proved
  wrong.
- **Max holdings (swing buyer)** — a cap specific to this engine; it won't open
  a new name beyond this count, independently of the exit engine's own
  max-holdings rule above.

**COPIER**
- **Daily copy budget $** — the total dollar budget available each day for
  copying politician disclosures, before it's split across the pool by
  rank/weight.
- **Consensus boost ×** — the size multiplier when 2+ pool members disclose
  the same ticker within 14 days. At `3` here, a consensus buy gets three times
  the size a single-member disclosure would.
- **Max disclosure age (days)** — the signal-decay cutoff: disclosures filed
  longer ago than this are skipped as too stale to still carry an edge.
- **Sentiment veto** — when ON, a strongly bearish LLM sentiment score can
  block a copy buy outright; off here means sentiment only ever scales size
  down, never vetoes the trade entirely.
- **Target sectors (csv)** — the sector whitelist for *new* copy buys —
  `tech, robotics, energy, industrial` here, matching this wallet's "High Risk"
  thematic focus. Also the list EXITS' "Prune off-sector holdings" checks
  against, if that's ever switched on.

**SCHED**
- **Exit engine cadence (min)** — how often the exit engine re-checks every
  held position for stop/trail/take-profit/pyramid conditions during market
  hours. At `20`, this wallet's positions get a fresh look roughly 19-20 times
  across a 6.5-hour trading day.
- **Swing buy time (ET)** — the one daily window, Eastern time, when the swing
  buyer's ranking-and-entry logic runs.
- **Copy loop time (ET)** — the one daily window when the disclosure copier
  checks for and copies fresh politician trades.
- **Daily window width (min)** — how wide the two windows above actually are;
  the scheduler's heartbeat has to land inside this window from its start time
  for that day's run to fire at all.
- **Weekdays only** — when ON, Saturday/Sunday ticks are skipped entirely,
  since the underlying markets are closed anyway.

---

## In-App Manual (`h`)

Press `h` anywhere in the TUI to open this README **inside the dashboard**, rendered
with Textual's `MarkdownViewer` — tables, headings, and links render as formatted
text, and a **table-of-contents sidebar is generated automatically from every
heading in this file**. Use ↑/↓ to move through the topic index, **Enter** to jump
straight to a section, arrow keys / Page Up / Page Down to scroll the body, and
**Esc** to close. You never need to leave the terminal or locate this file on disk —
whatever's in `README.md` is what you see, always current with the repo.

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
| `tui.py` | **Interactive trading cockpit** — live Textual dashboard; monitors account/positions, charts holdings or portfolio equity across 1D-1Y, and can flatten, rebalance, run strategy cycles, or place manual buy/sell orders behind arm + confirm gates. Also hosts the strategy config editor (`n`) and in-app manual (`h`). |
| `trading_scheduler.py` | **Autonomous engine dispatcher** — 10-min launchd heartbeat; self-gates to market hours, then loops over every configured wallet: binds that wallet's credentials + strategy context and fires its enabled engines on its own cadences. Master switches live in each wallet's `strategy_<slug>.json`. |
| `swing_buyer.py` | **Autonomous buy engine** — daily regime-filtered relative-strength entries + dip-adds on proven winners. `--dry-run` / `--rank` / `--force` modes. |
| `backtest_swing.py` | No-lookahead historical backtest of the swing-buyer + exit-engine rules (daily bars, fills at next-open, slippage modeled). |
| `hermes_report.py` | Legacy single-wallet report + chart/data hub — market/account helpers used by the TUI, Telegram splitter, chart PNGs. CLI-only for reporting since the multi-wallet switch. |
| `wallet_report.py` | **Multi-wallet Telegram report** — leaderboard + combined summary + per-wallet day P&L/movers/fills, per-request credentials, bilingual (follows `tui.language`). Pushed by the TUI tick, `p`, the scheduler, or CLI. |
| `report_scheduler.py` | Config-driven report trigger; launchd fires a heartbeat, then this script gates on `report_schedule` in `strategy_config.json` and dedupes to one report/day (stamp shared with the TUI's in-process tick). |
| `capitol_copier.py` | Scrapes Capitol Trades and copies qualifying politician buys/sells to Alpaca; also runs the dynamic exit engine (stop/trail/take-profit/pyramid). Modes: `--manage-only` (exits only), `--sync-state` (reconcile dedup state, no trades), `--dry-run`, `--rebalance`. |
| `intraday_momentum.py` | Separate 4x-leveraged intraday day-trading strategy (behind the TUI `t` key). Off by default — see the `intraday.*` note in [Strategy Config Editor](#strategy-config-editor-n). |
| `rebalance_top_n.py` | Rebalance helper used by the TUI to keep top performers, raise cash, or deploy idle cash. |
| `config_io.py` | Shared config read/write helper — atomic per-key updates used by the TUI schedule/channel/strategy editors. |
| `config_fields.py` | Declarative registry of every field the TUI strategy config editor (`n`) exposes: bounds, danger flags, descriptions. |
| `sectors.py` | Ticker → sector map used for the copier's `target_sectors` whitelist and prune logic. |
| `pool_manager.py` | Manages pool membership, trade sizing, consensus detection, and exposure limits. |
| `politician_vetter.py` | Scores politicians and selects the active pool. |
| `politician_history.py` | Tracks per-politician pool history and probation weeks. |
| `backtest_engine.py` | Backtests politician trades vs SPY across multiple hold windows. |
| `performance_tracker.py` | Syncs filled Alpaca orders, pairs buy/sell trades, and computes realized P&L vs SPY. |
| `sentiment_check.py` | Fetches ticker news headlines and scores sentiment. |
| `analyst_llm.py` | Analyst-summary client for report commentary — Codex Terra via Hermes, NVIDIA Nemotron 120B fallback. No OpenRouter, no API key in this repo. |
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
| `strategy_config.json` | Global app settings only: `tui`, `telegram`, `report_schedule`, `wallets` registry |
| `strategy_high_risk.json` / `strategy_low_risk.json` | **Per-wallet strategy parameters** (pool, exits, swing, copier, schedule) — edited via `n` while viewing that wallet |
| `strategies.py` | Per-wallet config/state resolution layer (`load_merged`, `update_strategy`, `state_path`) |
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

## Daily Telegram Report — multi-wallet (`wallet_report.py`)

The scheduled push is **owned by the running TUI**: a once-a-minute tick fires
`wallet_report.send_wallets_report()` inside the `report_schedule` window
(`strategy_config.json`; edit with `g`). The launchd heartbeat
(`report_scheduler.py`) fires the same report and shares the same
`.report_schedule_state.json` dedup stamp, so the two paths can never
double-push. Manual push: TUI `p`, or `python3 wallet_report.py`
(`--no-push` to preview).

Every configured wallet is fetched with per-request credentials
(compare.py pattern — the active wallet is never disturbed):

```
📊 Alpaca Wallets Report
🏆 LEADERBOARD (vs $100k)       ranked table, ● = active wallet
ALL WALLETS (n)                 combined equity / day P&L / vs base / n▲ n▼
💬 analyst take                 one Hermes call (Codex Terra) over the whole book
━━ per wallet ━━                day P&L · equity · top-3 movers
  Trades today                  HH:MM B/S SYM qty @price (buy+sell fills)
```

The body follows the TUI language (`tui.language`, en/繁體中文). The same run
writes `reports/wallets_*.md`. The legacy single-wallet report
(`hermes_report.py`, 4 messages + 2 chart PNGs) remains available from the CLI.

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
├── trading_scheduler.py      # autonomous engine dispatcher (10-min heartbeat)
├── swing_buyer.py            # autonomous RS-momentum + dip-add buy engine
├── backtest_swing.py         # no-lookahead backtest for swing_buyer + exits
├── hermes_report.py          # report builder + chart/account helpers
├── report_scheduler.py       # config-driven daily report trigger
├── capitol_copier.py         # smart money copy engine + dynamic exit engine
├── intraday_momentum.py      # intraday RS day-trading strategy (4x)
├── rebalance_top_n.py        # TUI rebalance helper
├── config_io.py              # shared config persistence
├── config_fields.py          # strategy config editor field registry (TUI 'n')
├── sectors.py                # ticker → sector map (copier whitelist)
├── pool_manager.py           # pool membership + trade sizing
├── politician_vetter.py      # weekly pool scoring
├── politician_history.py     # pool history tracker
├── backtest_engine.py        # historical backtesting
├── performance_tracker.py    # P&L sync + benchmarking
├── sentiment_check.py        # LLM sentiment scoring
├── analyst_llm.py            # analyst commentary (Codex Terra → Nemotron)
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
