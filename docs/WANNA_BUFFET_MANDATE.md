# Wanna_buffet — Photonic CPO ETF client mandate

## Status and activation boundary

This document records the agreed client mandate. It does not enable a trading
engine, change strategy files, install a schedule, or configure a Telegram
poller. Autonomous Wanna_buffet execution under this mandate is NOT deployed.
The wallet already has legacy strategy settings; do not interpret this document
as proof that all existing account activity is disabled.

Activation requires implementation, offline safety tests, read-only account and
position reconciliation, verified single-writer ownership, and a paper-only
rollout. Never silently switch to a live endpoint. No commitment is made to a
particular market opening before those checks pass.

## Account scope — verified with a read-only broker request

- Wallet label: `Photonic CPO ETF`.
- Exact Alpaca `account_number`: `PA3BZK0WN833` (not the account UUID).
- Endpoint: `https://paper-api.alpaca.markets/v2`.
- Account observed ACTIVE, with `trading_blocked: false` and
  `shorting_enabled: true`. Broker eligibility does not authorize strategy shorts.
- Existing credential references:
  `ALPACA_API_KEY_PHOTONIC_CPO_ETF` and
  `ALPACA_SECRET_KEY_PHOTONIC_CPO_ETF`.
- Do not change High Risk or Low Risk wallet strategies, credentials, ownership,
  existing reports, or unrelated working-tree edits.

## Investment mandate — agreed with Peter

- Initial pilot: long-only; opening short positions is disabled.
- No leverage. Proposed purchases must not create borrowing or take combined
  long exposure, including pending opening orders, above wallet equity.
- New purchases/additions must not take a stock above 20% of wallet equity.
  Pre-existing overweight holdings are not automatically liquidated solely to
  satisfy the new purchase ceiling; they may be trimmed under the strategy.
- All existing holdings may be trimmed or sold. There are no protected core
  positions or client-imposed ticker exceptions.
- Begin with existing holdings and the predominantly technology-oriented
  portfolio. Broadening across US sectors is a later phase, not immediate
  unrestricted universe expansion.
- Execute only during the regular US equity session, respecting broker calendar
  holidays, early closes, halts, and order eligibility.
- Long positions may be held overnight and for extended periods when their
  investment thesis and portfolio risk remain acceptable.
- A 10% return on starting wallet equity over the first three months is a stretch
  objective, not a guarantee and not permission to relax risk controls.
- Record a start baseline at activation; account for deposits/withdrawals,
  dividends, execution costs, and both realized and unrealized results when
  reporting performance. Compare against an investable SPY total-return baseline.

## Drawdown and delegated recovery policy

- Pause new risk when wallet equity drawdown reaches 5% from its high-water mark.
- Continue protective exits, reconciliation, and other risk-reducing management.
- Peter delegated recovery mechanics; no per-trade or daily restart approval is
  required within the mandate.
- Selected initial recovery rule: resume purchases only after drawdown is below
  3% from the SAME high-water mark for two consecutive completed sessions, with
  fresh data and healthy execution. Cash-flow adjustments must be economically
  correct; never reset a peak simply to bypass the pause.
- If recovery conditions do not occur, remain paused. A goal shortfall or elapsed
  cooldown alone cannot override the drawdown rule.
- Data/account/order uncertainty blocks new risk; do not infer an empty portfolio
  from an API failure.

## Research and adaptation — proposed inputs, not deployed feeds

- Macro: FRED, BLS, and BEA for rates/credit conditions, inflation, employment,
  growth, and relevant economic releases.
- Market/sector: Alpaca price, volume, quote/liquidity, and sector-trend data,
  subject to verified data entitlements and freshness.
- Company: SEC filings, issuer earnings releases/guidance, and verified news
  for business fundamentals and catalysts.
- Use point-in-time data, including release/revision timestamps. No future
  information, retrospective constituent selection, or incomplete-bar leakage.
- An LLM may synthesize evidence and propose tactics; independent deterministic
  controls enforce account identity, exposure, trading hours, drawdown, and
  allowed order actions.
- Log evidence, decisions, rejected proposals, orders, fills, attribution,
  portfolio outcomes, and policy versions. Evaluate adaptations out of sample
  and net of costs rather than using narrative confidence or win rate alone.
- Tactical adaptations are autonomous within tested, bounded policies. They
  cannot remove protection, increase hard risk ceilings, broaden the approved
  rollout scope, or move to real-money trading without a mandate change.
- Peter delegates the eventual decision to introduce shorting instead of
  approving individual short trades. The initial pilot remains long-only;
  automatic introduction of shorts requires a separately tested borrowing,
  whole-share sizing, margin, squeeze-risk, and protective-BUY policy. No
  short-opening execution path is currently enabled by this document.

## Client relationship and Telegram routing

- Peter is the client; Wanna_buffet manages routine decisions within the mandate.
- Consultative reviews every TWO WEEKS (not twice a week); no routine requests
  for permission to trade or handoff of daily recovery decisions to Peter.
- Discuss outcomes, benchmark-relative returns, drawdown, major decisions,
  evidence, failures, and proposed higher-level changes; explain the strategy
  in accessible language.
- User-specified bot: `@Panbear_Hermes_bot`, bot ID `8370657441`.
- Identity verified with Telegram `getMe`: exact username and ID match.
- Existing configured destination verified with `getChat`: private chat
  `7512954760`. Peter confirmed the existing bot conversation as the review
  destination; the BOT ID must never be used as a recipient chat ID.
- Peter authorized enabling the mandated PAPER strategy after the implementation
  and safety checks pass. This authorization is not evidence of deployment and
  does not authorize real-money trading.
- No messages were sent, review schedules installed, or bot pollers started
  during these routing checks. Preserve the existing inbound bot ownership;
  never start a competing poller or redirect another wallet's notifications.

## Remaining engineering work

1. Pin exact paper endpoint and broker account identity at all execution paths.
2. Implement independent long-only/no-leverage/exposure/pending-order controls.
3. Implement durable drawdown baseline, cash-flow handling, and recovery state.
4. Verify research feed access, point-in-time behavior, and missing-data policy.
5. Build and test regime, allocation, thesis/exit, and bounded-adaptation logic.
6. Add durable execution state, broker-held protection, partial-fill handling,
   deduplication, restart recovery, and complete audit/performance records.
7. Confirm Telegram destination, trace the actual existing two-way bot route,
   and implement the review flow without replacing established delivery paths.
8. Run offline tests, shadow evaluation, and a gated autonomous paper rollout.
9. Consider live funds only after sustained evidence across market conditions;
   live activation remains a separate explicit client decision.

## Initial learning implementation — staged, not live

`wb_learning.py` implements a durable, idempotent SQLite evidence ledger for
future decisions, orders, fills, equity snapshots, evaluations, and rejections.
It rejects conflicting replays, malformed timestamps, non-finite JSON values,
and recognized credential-field keys; its database file is owner-only. It is
not yet wired to execution, so no claim is made that current trades are captured.

The selected self-improvement approach is bounded policy evaluation rather than
online reinforcement learning from a small, noisy trading sample. Proposed
changes need sufficient comparable net-of-cost outcomes, held-out/walk-forward
validation, and checks for drawdown and regime dependence. Candidate policies
must first run in shadow; rollback and complete attribution are required before
automatic promotion among approved policies. The 10% profit objective does not
permit changing account scope or hard risk limits.

Current-vintage FRED access was exercised successfully for DFF, CPIAUCSL, and
UNRATE. This verifies retrieval only, not a deployed macro feed or point-in-time
historical backtest. Store retrieval/release provenance before using these
observations in decisions or historical evaluation.

The safety-domain files left by the interrupted session-local worker were
independently inspected and tested after it stopped. The focused safety,
ceiling, learning, and release-before-sell command passed 233 tests; the full
`python3 -m pytest tests/ -q` run passed 368 tests with 11 matplotlib/pyparsing
deprecation warnings, and `git diff --check` passed. This verifies the current
offline foundation only, not account-writer integration or activation. The
builder has received the exact API signatures, source hashes, and remaining
adapter/protection requirements.

## Durable build-to-activation pipeline

- Build card: `t_fd30dced`, builder in an isolated worktree, pinned to
  `openai-codex / gpt-6.1-sol`, with bounded goal-mode iterations.
- Verification/integration/activation card: `t_2649dfca`, orchestrator in the
  live repository, dependency-gated on the build card's completion.
- Initial run 11 was incorrectly treated as executing because the board said
  RUNNING. Subsequent process/log checks proved it exited at startup with
  `ModuleNotFoundError: No module named 'hermes_cli'` and made no worktree edits.
  The stale run was reclaimed, and run 12 was dispatched using the supported
  `HERMES_BIN=/Users/peter/.local/bin/hermes` launcher override. Run 12 has an
  actual live process, heartbeat, executed tools, and copied foundation files
  in `.worktrees/t_fd30dced`. The earlier time estimate is no longer reliable.
  The activation card is still dependency-gated; no activation has occurred.
  The launch override applies to this CLI dispatch only: the existing gateway's
  future worker-launch environment has not been repaired or verified.
- The loaded Hermes gateway's home was verified as
  `/Users/peter/Agents/hermes/hermeshome`, its Kanban dispatch setting was true,
  and its board view included this running build. This is durable queued work,
  not the previous session-local delegated safety-module worker, which was
  interrupted after exceeding its bounded scope.
- The integration card has authorization to activate only after the specified
  tests, shadow run, account/data checks, and single-writer checks pass, and to
  report the factual outcome through the existing approved Telegram route.
  Failure, missing capability, or iteration exhaustion is not permission to
  bypass those checks. Initial execution remains paper-only and long-only.
