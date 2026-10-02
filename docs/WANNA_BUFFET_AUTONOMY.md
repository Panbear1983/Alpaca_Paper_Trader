# Wanna Buffet — trading its own wallet, learning as it goes

Peter's instruction, 2026-10-02: the Hermes agent "Wanna Buffet" trades the wallet
**Wanna_Buffet Auto Trading** (Alpaca paper account PA3BZK0WN833, formerly "Photonic CPO ETF")
on its own, aims at **+10% of starting equity every three months**, iterates its own rules over
time and documents everything. **No guardrails are imposed from outside.** Every limit the code
enforces on this wallet is read from the agent's own strategy file, which the agent edits itself.

This document is the operating manual, for Peter and for the agent.

## The loop

| when (New York) | job | what happens |
|---|---|---|
| every 15 min, 09:30–16:00, weekdays | `wbauto_session` | `wbauto.py scan` prints the market read, the book, the rules in force and the target. The agent decides: buy, sell or hold, through `wbauto.py buy/sell`. Outside market hours the scan prints nothing and the agent is not woken. |
| 17:35 (05:35 Taipei), Tue–Sat | `wbauto_review` | `wbauto.py review` prints the day's facts. The agent judges the day, may change at most ONE rule with a written reason, writes its journal, and the 100-word summary is delivered to Peter on Telegram. |
| Saturday 10:00 Taipei | `wbauto_weekly` | `wbauto.py review --weekly` — the same, over the week, plus the quarter-to-date against QQQ and SPY. |

The old `anchor_session` job (scanner-bound, High Risk plan) is paused for good.

## The tools (one file: `wbauto.py`)

Always run as `cd /Users/peter/GitHub/Alpaca_Paper_Trader && venv/bin/python wbauto.py <command>`.

- `scan` / `status` — read the market and the book.
- `buy SYM --usd N [--stop S] --why "..."` — any name, any size the agent chooses. The order
  passes through the fence **with the agent's own rules** (its list, window, caps, cash floor,
  cooling-off, regime filter). A refusal is printed as `BLOCKED by your own rule — …`; the agent
  may change that rule.
- `sell SYM [--frac F | --usd N] --why "..."`.
- `tune show|get|set|add-name|remove-name` — edit `strategy_wanna_buffet_auto_trading.json`.
  `--why` is mandatory; every change lands in `diary/wbauto/changes.jsonl`.
- `journal "text"` — the agent's diary, `diary/wbauto/journal.md`.
- `review [--weekly]`, `target [--reset]`.

Orders carry the tag `wbauto`, so every fill is attributable to the agent (the scorecard and the
performance tracker know the tag).

## What is enforced, and by whom

- The entry fence (`entry_gate.py`) and the caps (`capitol_copier.py`) enforce **the agent's own
  file**. There is no code ceiling for this wallet (`HARD_LIMITS` has no row for it).
- The exit engine (`capitol_copier.manage_open_positions`) runs every 20 minutes from the trading
  scheduler **with the agent's own exit settings** (stop, trail, profit tiers, pyramid adds). The
  agent may change or switch it off.
- The swing buyer and the copier are switched off on this wallet at handover so that the agent is
  the only trader. The agent may switch them on as its own tools.
- There are **no resting stops at the broker** on this wallet and no 30-second price watcher. The
  agent knows this. If it wants broker-side stops it asks Peter.
- The account allows margin (4x) and shorting. Nothing from outside stops the agent from using
  margin; its gross cap is in its own file. Shorting tools are not built; it may ask.

## What is documented, where

- `diary/wbauto/journal.md` — the agent's reasoning after every review.
- `diary/wbauto/trades.jsonl` — every buy/sell it sent, with its reason and its intended stop.
- `diary/wbauto/changes.jsonl` — every rule change, old → new, with the reason.
- `strategy_wanna_buffet_auto_trading.json` — its rules in force; `updated_by` names the agent.
- The Hermes profile's `cron/output/<job>/` — the full text of every scheduled run.
- `diary/guardrail_log.jsonl` — refusals by its own rules (rows carry `wallet`).

## Peter's controls

- Pause everything: `hermes -p wanna_buffet cron pause <job id>` (ids in `hermes -p wanna_buffet cron list --all`).
- Resume: `cron resume <job id>`.
- The dashboard shows the wallet; the strategy tab (`n`) shows its file — editing it there is
  Peter overriding the agent, and the agent will see the change in its next scan.
- Claude observes and reports on request; it does not change this wallet's rules.
