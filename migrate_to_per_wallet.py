"""
migrate_to_per_wallet.py — one-shot split of the unified strategy_config.json
into per-wallet strategy files. Idempotent: refuses to run twice.

  strategy_high_risk.json   ← strategy sections VERBATIM (zero behavior change)
  strategy_low_risk.json    ← same shape, every engine switch forced OFF
  strategy_config.json      ← global-only (tui/telegram/report_schedule/wallets/meta)
  .copied_trades.json etc.  ← renamed to the default wallet's slug

Backups: strategy_config.json.bak_pre_split (full pre-split config).
Run:  python3 migrate_to_per_wallet.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys

import strategies
import wallets

HERE = os.path.dirname(os.path.abspath(__file__))
GLOBAL_FILE = os.path.join(HERE, "strategy_config.json")

# Engine master switches forced OFF in every non-default wallet's initial file.
_OFF_SWITCHES = (
    ("swing", "enabled"),
    ("capitol_copier", "autorun_enabled"),
    ("intraday", "enabled"),
)

# Engine state files that must become per-wallet (owned by the default wallet
# today — its engines are the only ones that ever ran).
_STATE_FILES = (
    ".copied_trades.json",
    ".position_state.json",
    ".swing_state.json",
    ".intraday_state.json",
)


def _write_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main() -> int:
    with open(GLOBAL_FILE) as f:
        cfg = json.load(f)

    default = wallets.default_name()
    names = [w["name"] for w in wallets.list_wallets()]
    if not any(k in cfg for k in strategies.STRATEGY_KEYS):
        print("already migrated — strategy sections not in the global file")
        return 0
    for name in names:
        if os.path.exists(strategies.path_for(name)):
            print(f"refusing: {strategies.path_for(name)} already exists")
            return 1

    # 1. Backup
    bak = GLOBAL_FILE + ".bak_pre_split"
    shutil.copy2(GLOBAL_FILE, bak)
    print(f"backup → {os.path.basename(bak)}")

    # 2. Per-wallet strategy dicts
    strat = {k: cfg[k] for k in strategies.STRATEGY_KEYS if k in cfg}
    meta = {k: cfg[k] for k in strategies.META_KEYS if k in cfg}

    for name in names:
        body = copy.deepcopy(strat)
        body_meta = dict(meta)
        if name != default:
            for sect, key in _OFF_SWITCHES:
                if sect in body:
                    body[sect][key] = False
            body_meta["updated_by"] = f"migrate_to_per_wallet: engines OFF for '{name}'"
        out = {**body_meta, **body}
        _write_json(strategies.path_for(name), out)
        switches = {f"{s}.{k}": body.get(s, {}).get(k) for s, k in _OFF_SWITCHES}
        print(f"wrote {os.path.basename(strategies.path_for(name))}  switches={switches}")

    # 3. Global file keeps only shared keys
    global_cfg = {k: v for k, v in cfg.items() if k not in strategies.STRATEGY_KEYS}
    _write_json(GLOBAL_FILE, global_cfg)
    print(f"rewrote strategy_config.json (global-only: {sorted(global_cfg)})")

    # 4. State files → default wallet's slug
    for base in _STATE_FILES:
        src = os.path.join(HERE, base)
        if os.path.exists(src):
            dst = strategies.state_path(base, default)
            os.replace(src, dst)
            print(f"state {base} → {os.path.basename(dst)}")

    print("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
