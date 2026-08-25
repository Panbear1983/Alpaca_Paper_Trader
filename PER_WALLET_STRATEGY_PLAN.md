# Per-Wallet Strategy Configuration — Design & Migration Plan

**Date:** 2026-07-09 · **Anchor commit:** `b3e1e68` (rollback: `git reset --hard b3e1e68`)

## Context

The TUI supports multiple Alpaca paper wallets (High Risk, Low Risk via the `k`
switcher), but strategy configuration is a single unified `strategy_config.json`.
Consequences today: the `n` editor edits High Risk's live trading behavior no
matter which wallet you're viewing; Low Risk cannot have its own strategy; the
autonomous engines can only ever serve one account. This build gives **each
wallet a standalone strategy file** while keeping shared app settings global.

## Target layout

| File | Contents |
|---|---|
| `strategy_config.json` (kept) | Global only: `tui`, `telegram`, `report_schedule`, `wallets` registry, version meta |
| `strategy_high_risk.json` (new) | Strategy sections, values copied **verbatim** from today's config |
| `strategy_low_risk.json` (new) | Same shape, **all engine switches OFF** (`swing.enabled`, `capitol_copier.autorun_enabled`, `intraday.enabled` = false) |

Strategy sections: `capitol_copier`, `pool`, `dynamic_exits`, `intraday`,
`scoring_weights`, `review`, `swing`, `trading_schedule` (+ per-file version meta).

Per-wallet files keep the **same internal shape**, so the 32-field editor
registry (`config_fields.py` dotted paths), validation, danger previews, and the
中文 field translations (`i18n._FIELD_ZH`) work unchanged.

## New module: `strategies.py`

- `slug(name)` — "High Risk" → `high_risk`
- `path_for(wallet)` — repo-root `strategy_<slug>.json` (root keeps Hermes
  guard-glob coverage)
- `set_active(name)` / `active()` — process-local wallet context
  (default: `wallets.default_name()`)
- `load_strategy(wallet=None)` — the wallet's file only (for read-modify-write)
- `load_merged(wallet=None)` — global ∪ wallet file. **This is what legacy
  `load_config()` callers get**, so every existing read site (both global keys
  like `tui`/`telegram` and strategy keys like `pool.*`) keeps working.
- `update_strategy(mutator, wallet=None)` — atomic write to the wallet file via
  `config_io.update_config(path=...)`
- `state_path(basename, wallet=None)` — `.copied_trades.json` →
  `.copied_trades.high_risk.json` etc.

## Rewiring (13 modules)

| Module | Change |
|---|---|
| `capitol_copier` | `load_config()` → `strategies.load_merged()`; `STATE_FILE`/`POS_STATE_FILE` constants → `strategies.state_path(...)` lookups inside load/save fns |
| `swing_buyer` | inherits capitol's loader; own `STATE_FILE` → `state_path(".swing_state.json")` |
| `intraday_momentum` | loader → merged; state → per-wallet |
| `trading_scheduler` | **loop over configured wallets**: `strategies.set_active(w)` → `wallets.apply(w)` (cred rebind — the existing verified mechanism) → run only that wallet's enabled engines → per-wallet idempotency stamps inside `.trading_schedule_state.json` |
| `tui.py` | `n` editor reads/writes the **active wallet's** file (`strategies.update_strategy`); header shows the wallet name; `_do_switch_wallet` also calls `strategies.set_active`; `g`/`m`/`l` keep writing the global file |
| `sunday_review` | `load_config`/`save_config` → `load_strategy`/`update_strategy` (wallet-only dict for its whole-dict read-modify-write; prevents global keys leaking into wallet files) |
| `pool_manager`, `politician_vetter`, `backtest_engine`, `backtest_swing` | loader → merged (read-only callers) |
| `hermes_report` | `load_local_state()["config"]` → merged view of the wallet being reported |
| `report_scheduler` | no change (reads `report_schedule` from the global file) |
| `wallets.rename` | additionally moves the wallet's strategy + state files |
| `propose_change` | sanity gate: every `strategy*.json` parses; per-wallet `dynamic_exits.stop_loss_pct` ∈ [0.02, 0.25] (replaces the dead `tsla.stop_loss_pct` check) |
| `config_io` | unchanged (already path-parameterized, atomic os.replace) |

## Migration (one-shot script `migrate_to_per_wallet.py`)

1. Backup `strategy_config.json` → `strategy_config.json.bak_pre_split`
2. Write `strategy_high_risk.json` = current strategy sections verbatim
3. Write `strategy_low_risk.json` = same, engine switches forced OFF
4. Rewrite global file without strategy sections
5. Rename existing state files to the High Risk slug
6. Idempotent: refuses to run twice

## Safety invariants

- **Day-one behavior identical.** High Risk engines read byte-identical values;
  Low Risk is fully off until its own switches are flipped in `n` while viewing it.
- **No cred/config cross-wiring.** An engine only runs for wallet W after
  `wallets.apply(W)` has rebound credentials — Low Risk config can never drive
  High Risk orders.
- **No state cross-contamination.** Dedup tx_ids, trail peaks, cooldowns, and
  scheduler stamps are all per-wallet.
- Scheduler runs engines **sequentially per wallet** in one process; the TUI is
  a separate process — its active-wallet rebinding never interferes.

## Verification

1. `py_compile` all touched modules
2. Migration on a copy → diff proves High Risk values verbatim + global file
   clean
3. Isolation test: `update_strategy` on Low Risk → High Risk file byte-identical
4. Scheduler dry loop (fake wallets/engines) → per-wallet cred rebind + state
   isolation asserted
5. `capitol_copier --manage-only --dry-run` + `swing_buyer --dry-run` under
   default wallet → identical decisions to pre-split
6. TUI pilot: `n` header names active wallet; edit under Low Risk → only
   `strategy_low_risk.json` changes
7. Live TUI drive + regression of existing headless suites (lang toggle, compare)
