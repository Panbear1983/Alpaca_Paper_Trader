"""
wallets.py — switch the live Alpaca account the TUI reads/acts on, at runtime.

Every trading module bakes ALPACA credentials into module-level constants at
import time (hermes_report.HEADERS / ALPACA_BASE, capitol_copier.ALPACA_HEADERS /
BASE_URL, and imported copies in intraday_momentum / swing_buyer). Because the
API-calling functions resolve those names as *module globals at call time*,
reassigning them redirects every subsequent request to a different paper
account — no re-import, no restart.

Wallets are declared in strategy_config.json under a "wallets" block that
references ENV-VAR NAMES only (never the secrets themselves) — the exact pattern
the "telegram" channels block already uses. A wallet is "configured" only when
its key + secret env vars are actually present, so a wallet can be listed but
inert until you add its credentials to .env.

SCOPE (important): this switches what the *TUI* views and what its *manual*
actions (buy/sell/flatten/rebalance/tick/capitol/report) target. It does NOT
redirect the autonomous engines — trading_scheduler / swing_buyer / capitol
copier run as separate launchd processes reading .env fresh each tick, so they
always trade the .env (default) account. Keep that separation in mind.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent

# Mirror hermes_report's env loading so wallet creds resolve identically whether
# this module is imported first or not.
load_dotenv(HERE / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

CONFIG_FILE = HERE / "strategy_config.json"

_DEFAULT_BASE = "https://paper-api.alpaca.markets/v2"

# Fallback registry if strategy_config.json has no "wallets" block: the existing
# single account, driven by the standard env vars.
_FALLBACK = {
    "default": "Main",
    "accounts": {
        "Main": {"key_env": "ALPACA_API_KEY",
                 "secret_env": "ALPACA_SECRET_KEY",
                 "base_env": "ALPACA_BASE_URL"},
    },
}

# Active wallet name (module-global; starts at the config default).
_current: str | None = None


def _load_block() -> dict:
    import json
    try:
        with open(CONFIG_FILE) as f:
            blk = json.load(f).get("wallets") or {}
    except Exception:
        blk = {}
    if not blk.get("accounts"):
        return _FALLBACK
    blk.setdefault("default", next(iter(blk["accounts"])))
    return blk


def default_name() -> str:
    return _load_block()["default"]


def current() -> str:
    global _current
    if _current is None:
        _current = default_name()
    return _current


def _spec(name: str) -> dict | None:
    return _load_block()["accounts"].get(name)


def missing_envs(name: str) -> list[str]:
    """Env-var names required by `name` that are not set (empty = configured)."""
    spec = _spec(name)
    if not spec:
        return ["<unknown wallet>"]
    out = []
    for field in ("key_env", "secret_env"):
        var = spec.get(field, "")
        if not var or not os.getenv(var):
            out.append(var or f"<{field}>")
    return out


def is_configured(name: str) -> bool:
    return not missing_envs(name)


def resolve(name: str) -> tuple[str, str, str] | None:
    """(key, secret, base_url) for a wallet, or None if creds are missing."""
    spec = _spec(name)
    if not spec:
        return None
    key = os.getenv(spec.get("key_env", ""), "")
    secret = os.getenv(spec.get("secret_env", ""), "")
    base = os.getenv(spec.get("base_env", ""), "") or _DEFAULT_BASE
    if not (key and secret):
        return None
    return key, secret, base.rstrip("/")


def list_wallets() -> list[dict]:
    """[{name, configured, is_current, missing}] for every declared wallet."""
    blk = _load_block()
    cur = current()
    out = []
    for name in blk["accounts"]:
        miss = missing_envs(name)
        out.append({
            "name": name,
            "configured": not miss,
            "is_current": name == cur,
            "missing": miss,
        })
    return out


def rename(old: str, new: str) -> tuple[bool, str]:
    """Rename a wallet's display label in strategy_config.json (atomic). The
    env-var references and credentials are untouched — only the name changes.
    Updates the config default and the active-wallet pointer if they referred
    to `old`. Returns (ok, message)."""
    global _current
    new = (new or "").strip()
    if not new:
        return False, "name cannot be empty"
    accounts = _load_block()["accounts"]
    if old not in accounts:
        return False, f"'{old}' not found"
    if new == old:
        return True, "unchanged"
    if new in accounts:
        return False, f"'{new}' already exists"

    import config_io

    def mut(cfg):
        w = cfg.setdefault("wallets", {})
        accts = w.get("accounts") or {}
        # rebuild preserving insertion order, swapping only the renamed key
        w["accounts"] = {(new if k == old else k): v for k, v in accts.items()}
        if w.get("default") == old:
            w["default"] = new
        return cfg

    try:
        config_io.update_config(mut)
    except Exception as e:
        return False, f"config write failed: {e}"

    if _current == old:
        _current = new
    return True, f"renamed '{old}' → '{new}'"


def apply(name: str) -> tuple[bool, str]:
    """Rebind Alpaca credential globals across every holder module so all
    subsequent API calls target `name`. Returns (ok, message)."""
    global _current
    creds = resolve(name)
    if creds is None:
        miss = ", ".join(missing_envs(name))
        return False, f"'{name}' not configured — set in .env: {miss}"
    key, secret, base = creds

    # hermes_report uses an "Accept" header; capitol_copier uses "Content-Type".
    hr_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                  "Accept": "application/json"}
    cc_headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                  "Content-Type": "application/json"}

    # hermes_report — the TUI's primary read path (account/positions/orders/charts)
    try:
        import hermes_report as hr
        hr.ALPACA_KEY, hr.ALPACA_SECRET, hr.ALPACA_BASE = key, secret, base
        hr.HEADERS = hr_headers
    except Exception as e:
        return False, f"hermes_report rebind failed: {e}"

    # capitol_copier — the order path (place_market_order, get_positions, …)
    try:
        import capitol_copier as cc
        cc.API_KEY, cc.SECRET_KEY, cc.BASE_URL = key, secret, base
        cc.ALPACA_HEADERS = cc_headers
    except Exception as e:
        return False, f"capitol_copier rebind failed: {e}"

    # Modules that imported COPIES of cc's constants (`from capitol_copier import
    # BASE_URL, ALPACA_HEADERS`) need their own names rebound — reassigning cc's
    # globals does not reach them. Only rebind if already imported.
    import sys
    for modname in ("intraday_momentum", "swing_buyer"):
        mod = sys.modules.get(modname)
        if mod is not None:
            if hasattr(mod, "BASE_URL"):
                mod.BASE_URL = base
            if hasattr(mod, "ALPACA_HEADERS"):
                mod.ALPACA_HEADERS = cc_headers

    _current = name
    return True, f"active wallet → {name}"
