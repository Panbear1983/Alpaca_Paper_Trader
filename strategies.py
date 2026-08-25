"""
strategies.py — per-wallet strategy configuration resolution.

Each wallet declared in strategy_config.json's "wallets" block owns a standalone
strategy file at the repo root: strategy_<slug>.json (e.g. strategy_high_risk.json).
The global strategy_config.json keeps ONLY shared app settings (tui, telegram,
report_schedule, the wallets registry, version meta).

Two read views:
  load_strategy(wallet)  — the wallet's own dict ONLY. Use for read-modify-write
                           (sunday_review, the TUI editor) so global keys never
                           leak into wallet files.
  load_merged(wallet)    — global ∪ wallet file. This is what legacy
                           `load_config()` callers receive, so read sites keep
                           working whether they want global keys (tui, telegram,
                           report_schedule) or strategy keys (pool.*, swing.*).

Writes stay split: config_io.update_config → global file;
update_strategy(...) → the wallet's file (same atomic os.replace mechanics).

Process-local wallet context: set_active(name) is called by the TUI on wallet
switch and by trading_scheduler per wallet in its loop; everything else defaults
to the config's default wallet, which preserves pre-split behavior for every
standalone script (report_scheduler, backtests, cron runs).

State files are per-wallet too: state_path(".copied_trades.json") →
.copied_trades.high_risk.json — dedup ids, trail peaks, and cooldowns must never
cross accounts.
"""
from __future__ import annotations

import json
import os
import re

import config_io

HERE = os.path.dirname(os.path.abspath(__file__))

# Sections that belong to a wallet's strategy file. Everything else in the old
# unified config is global. (Meta keys version/last_updated/updated_by exist in
# BOTH: each wallet file carries its own copy for sunday_review's bump.)
STRATEGY_KEYS = (
    "capitol_copier", "pool", "dynamic_exits", "intraday",
    "scoring_weights", "review", "swing", "trading_schedule",
)
META_KEYS = ("version", "last_updated", "updated_by")

_active: str | None = None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def default_wallet() -> str:
    import wallets
    return wallets.default_name()


def active() -> str:
    return _active if _active is not None else default_wallet()


def set_active(name: str | None) -> None:
    """Set the process-local wallet context (None → back to config default)."""
    global _active
    _active = name


def path_for(wallet: str | None = None) -> str:
    return os.path.join(HERE, f"strategy_{slug(wallet or active())}.json")


def load_strategy(wallet: str | None = None) -> dict:
    """The wallet's own strategy dict ONLY (no global keys)."""
    with open(path_for(wallet)) as f:
        return json.load(f)


def load_merged(wallet: str | None = None) -> dict:
    """Global config ∪ the wallet's strategy file (wallet wins on overlap)."""
    cfg = config_io.load_config()
    try:
        cfg.update(load_strategy(wallet))
    except FileNotFoundError:
        # Pre-migration fallback: the unified file still holds everything.
        pass
    return cfg


def update_strategy(mutator, wallet: str | None = None) -> dict:
    """Atomically apply `mutator(strategy_dict)` to the wallet's file."""
    return config_io.update_config(mutator, path=path_for(wallet))


def state_path(basename: str, wallet: str | None = None) -> str:
    """Per-wallet engine-state file: '.copied_trades.json' →
    '<HERE>/.copied_trades.high_risk.json'."""
    stem, ext = os.path.splitext(basename)
    return os.path.join(HERE, f"{stem}.{slug(wallet or active())}{ext}")


def wallet_names() -> list[str]:
    import wallets
    return [w["name"] for w in wallets.list_wallets()]
