# Trading Logic Upgrade Report — 2026-07-02

Full audit + rebuild of the buy side. The account previously had **five sell
mechanisms and one slow buy signal**; it now has a systematic, price-driven,
risk-gated buy engine, a repaired self-improvement loop, and a single scheduler
that runs everything autonomously — **gated behind explicit go-live switches
that are currently OFF**. The TUI was not touched.

---

## 1. Weaknesses found (audit summary)

| ID | Weakness | Status |
|----|----------|--------|
| A1 | Exit cash never redeployed — money left the market and sat | ✅ fixed (swing_buyer redeploys) |
| A2 | Only buy signal = politician disclosures (slow, sparse, no fallback) | ✅ fixed (RS momentum entries) |
| A3 | Pyramid tiers permanently burned when blocked by exposure cap | ✅ fixed (tiers retry) |
| A4 | No dip-buying — winners only added on strength, never on pullbacks | ✅ fixed (dip-add logic) |
| A5 | RS-ranking machinery existed but only intraday, unused for swing | ✅ fixed (reused for daily) |
| B1-B3 | Exit engine + copy loop had NO scheduler — nothing ran | ✅ fixed (trading_scheduler) |
| C1 | `sunday_review` crashed (KeyError `cfg["tsla"]`) — self-improvement loop dead | ✅ fixed (guard) |
| C2 | `max_disclosure_lag_days` config existed but was never enforced | ✅ fixed (enforced, set 14d) |
| C3 | `performance_tracker` attribution stale — current book tagged "other" | ✅ fixed (derive by exclusion) |
| C4 | `.copied_trades.json` dedup gap → double-buy risk on first live run | ✅ fixed (`--sync-state`) |
| C5 | Buys ignored existing holdings → could exceed per-name cap | ✅ fixed (holding-aware sizing) |
| C6 | Sector map: COHR/HOOD/KDP/DHI/RPM unmapped → wrong prunes/blocks | ✅ fixed (honest mappings) |
| D1 | Exposure 93.5% vs 85% cap — breached, blocking all buys forever | ✅ resolved (cap → 95%, deliberate) |
| D2 | No market-regime filter — bought as eagerly in downtrends | ✅ fixed (SPY 50d-SMA gate) |
| D3 | Prune-off-target would liquidate 8/15 positions (~$47k incl. HOOD +12.7%) | ✅ defused (`prune_off_target: false`) |
| D4 | `max_holdings: 10` cap-tail would sell 5 more current positions | ✅ defused (→ 20) |
| D5 | `intraday.enabled: true` — its EOD flatten liquidates the ENTIRE account | ✅ defused (→ false) |

## 2. What changed, file by file

### New files
| File | Purpose |
|---|---|
| `swing_buyer.py` | **The smart buy engine.** Daily: SPY>50d-SMA regime gate → dip-adds on proven winners (peak ≥+8%, pulled back ≥4%, still above entry, 7d cooldown) → new entries from 20d relative-strength ranking (top names, RS>0 only, max 2/day) → all capped by cash reserve ($2k), exposure cap, per-name cap ($8k), holdings cap (20), and a 5-day no-rebuy cooldown after any stop. `--dry-run`, `--rank`, `--force` modes. |
| `trading_scheduler.py` | Single heartbeat dispatcher (launchd every 10 min, self-gates to ET market hours): exit engine every 20 min, swing buyer daily @10:00 ET, disclosure copier daily @10:30 ET. Both engines gated by config switches. Idempotent via `.trading_schedule_state.json`. |
| `backtest_swing.py` | Historical validation. No-lookahead (signal at close t, fill at open t+1), 5 bps slippage/side, exits mirror the live engine (stop/trail/rotate). Param-sweepable. |
| `launchd/com.alpacapapertrader.trading_scheduler.plist` | Ready-to-install heartbeat job. **NOT loaded** — installing it is the go-live step. |

### Modified files
| File | Change |
|---|---|
| `capitol_copier.py` | `--manage-only` (exit engine standalone, market-clock-gated, for the 20-min schedule); `--sync-state` (marks all visible pool disclosures as copied WITHOUT trading — closes the double-buy gap); disclosure freshness filter (`max_disclosure_lag_days` now enforced); holding-aware buy sizing (respects per-name cap vs existing position); pyramid tiers no longer burned when cap-blocked; stop/trail exits now record `_stopped` history for swing_buyer's cooldown. `run()` signature unchanged (TUI-compatible). |
| `strategy_config.json` | `prune_off_target: false` (protects the current book), `max_holdings: 20`, `max_total_exposure_pct: 0.95`, `intraday.enabled: false`, `max_disclosure_lag_days: 14`, new `swing` + `trading_schedule` blocks, `capitol_copier.autorun_enabled: false`. |
| `sectors.py` | Added honest mappings: COHR→tech, HOOD→finance, KDP/DHI→consumer, RPM→industrial. |
| `sunday_review.py` | Fixed the KeyError crash that killed the weekly self-improvement loop. |
| `performance_tracker.py` | Attribution derived by exclusion (TSLA/AAPL legacy, everything else = swing book) instead of a stale hardcoded set — feeds real metrics to sunday_review's adjustment rules. |

### Untouched (per your instruction)
`tui.py` — zero changes. All function signatures it imports (`cc.run`,
`cc.place_market_order`, `im.rank_universe`, `rb.build_plan`, …) preserved;
verified by importing the TUI after all edits.

## 3. Backtest results (debugged, honest read)

Method: daily bars (iex feed), signals on close, fills next open, 5 bps
slippage per side, exits identical to the live engine.

| Window | Strategy | SPY B&H | Alpha | MaxDD | Trades | WinRate | PF |
|---|---|---|---|---|---|---|---|
| 1yr (top6, stop 8%) | **+126.7%** | +19.4% | +107.3% | 16.1% | 56 | 51.8% | 3.36 |
| 1yr, top4 | +135.1% | +19.4% | +115.7% | 19.0% | — | 60.6% | 4.13 |
| 1yr, top8 | +96.8% | +19.4% | +77.4% | 18.1% | — | 51.9% | 2.95 |
| 1yr, stop 6% | +112.3% | +19.4% | +92.9% | 14.4% | — | 49.2% | 3.31 |
| **~2yr (700d) — most honest** | **+72.8%** | +28.6% | **+44.2%** | 24.0% | — | 43.5% | **1.83** |

**Debugging conclusions & caveats (read these):**
- Robust across params — every variant beats SPY, so it's not one lucky config.
- **The 1-year numbers are inflated** by (a) a strong momentum-friendly bull
  year and (b) **universe selection bias**: the 25-name universe was chosen in
  mid-2026 knowing which large caps are today's leaders. The 700-day window
  (+72.8%, PF 1.83, 24% DD) is the sober estimate; live results will likely be
  lower still.
- What IS validated: the *shape* of the edge — losers cut ~-8%, winners ride to
  trail exits, profit factor > 1.8 in every configuration, and the regime
  filter + stop discipline kept max drawdown in the 14–24% band while fully
  invested in 4–8 names.
- Win rate ~44–60% with PF 1.8–4.1 = classic momentum profile: it pays with a
  few big riders, not by being right often. Expect losing streaks.

## 4. How the pieces fit now

```
launchd heartbeat (10 min)
  └─ trading_scheduler.py  (ET market-hours gate + config switches)
      ├─ every 20 min  → capitol_copier --manage-only     [SELL/ADD side]
      │                   stops · trails · take-profits · pyramids
      │                   writes _stopped history
      ├─ daily 10:00   → swing_buyer.py                   [BUY side]
      │                   regime gate → dip-adds → RS entries
      │                   redeploys the cash the exits free up
      └─ daily 10:30   → capitol_copier (copy loop)       [BUY side #2]
                          fresh disclosures only (≤14d), holding-aware

weekly (manual or scheduled later)
  └─ sunday_review.py — now unbroken: reads real per-trade metrics
      (performance_tracker), adjusts daily budget by win rate,
      rebalances pool weights by 14d momentum, monthly re-vet
```

The buy/sell loop is now **closed**: exits free cash → swing buyer redeploys it
into the strongest names → exits manage those → repeat. That's the "more
aggressive, more autonomous buying" you asked for, with five independent safety
rails (regime gate, cash reserve, exposure cap, per-name cap, stop cooldown).

## 5. GO-LIVE runbook (nothing trades until you do this)

```bash
cd /Users/peter/GitHub/Alpaca_Paper_Trader && source venv/bin/activate

# 1. RECONCILE STATE FIRST (one time, mandatory — prevents double-buys)
python3 capitol_copier.py --sync-state

# 2. Sanity-check both engines dry
python3 capitol_copier.py --dry-run
python3 swing_buyer.py --dry-run

# 3. Flip the switches you want live in strategy_config.json:
#      "swing": { "enabled": true, ... }                → smart buy engine
#      "capitol_copier": { "autorun_enabled": true }    → exits + disclosure copies
#    (You can enable one without the other.)

# 4. Install the heartbeat (THE go-live step)
cp launchd/com.alpacapapertrader.trading_scheduler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.alpacapapertrader.trading_scheduler.plist

# Monitor: tail -f .logs/trading_scheduler.out.log   (+ Telegram batch alerts)
# Kill switch: launchctl unload ~/Library/LaunchAgents/com.alpacapapertrader.trading_scheduler.plist
```

Note: market hours are 21:30–04:00 Taipei. Your Mac's 04:05 pmset wake covers
the last-tick window; if the Mac sleeps mid-evening, ticks are skipped
harmlessly (idempotent state prevents double-fires on wake).

## 6. Long-term iteration plan

**Phase 1 — Shadow (week 1):** run with `swing.enabled=true` but
`capitol_copier.autorun_enabled=false`; watch swing entries only, small
`entry_size_usd` (e.g. $1500). Confirm Telegram flow + scheduler logs.

**Phase 2 — Full loop (weeks 2–4):** enable `autorun_enabled` so exits run
every 20 min. The GEV pyramid and MU stop become live immediately. Review the
4PM Telegram report daily; run `sunday_review.py` each weekend.

**Phase 3 — Tuning (month 2):** with ≥20 closed trades, let sunday_review's
win-rate rules adjust `daily_budget_usd`; manually sweep `backtest_swing.py`
params quarterly (`--top`, `--stop`, `--days 700`) and adopt only changes that
improve the 700-day PF, not the 1-year return.

**Phase 4 — Universe hygiene (quarterly):** the biggest honest risk is universe
staleness/selection bias. Refresh the 25-name list mechanically: top-25 by
dollar volume among S&P 500 names, no discretion. Re-run the 700d backtest
after every refresh.

## 7. Self-improvement loop (design already wired, now unbroken)

```
trades close → performance_tracker (correct attribution, fixed)
            → sunday_review (no longer crashes, fixed)
                ├─ win rate > 55% → daily_budget +$200 (cap $5k)
                ├─ win rate < 40% → daily_budget −$200 (floor $500)
                ├─ pool weights ← 14d momentum (±15%/wk smoothing, graduation)
                └─ monthly → politician_vetter full re-vet (90d alpha backtest)
```

**Recommended additions (next iteration, not built yet):**
1. Extend sunday_review with swing-specific rules: adjust `entry_size_usd` by
   swing-trade win rate; widen/tighten `stop_pct` by realized volatility.
2. Log every swing decision (entered/skipped + reason) to a decisions journal;
   monthly, backtest the skipped-vs-taken cohort to detect systematic bias.
3. Auto-run `backtest_swing.py --days 700` in sunday_review and Telegram the
   PF trend — parameter drift detection without human attention.
4. Regime granularity: add a "half-risk" state (SPY between 20d and 50d SMA →
   half-size entries) instead of the current binary gate.

---
*All engines verified by live dry-runs 2026-07-02. Backtest debugged for
lookahead, cash accounting, and slippage. Go-live switches OFF pending your
sign-off.*
