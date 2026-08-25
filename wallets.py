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

SCOPE: apply() switches what the calling PROCESS reads/acts on. In the TUI that
means the view + manual actions (buy/sell/flatten/rebalance/tick/capitol/report).
trading_scheduler calls the same apply() per wallet in its dispatch loop, so
each wallet's engines run with that wallet's own credentials — always paired
with strategies.set_active() for that wallet's strategy file; the two must be
switched together (see PER_WALLET_STRATEGY_PLAN.md).
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

    import strategies

    # Per-wallet files are keyed by slug(name). An empty slug (fully non-ASCII
    # name) or one that collides with another wallet's slug would make two
    # wallets share ONE strategy/state file — wallet A's engines running wallet
    # B's strategy. Refuse those names outright.
    new_slug = strategies.slug(new)
    if not new_slug:
        return False, f"'{new}' needs at least one ascii letter/digit (file slug)"
    if any(strategies.slug(n) == new_slug for n in accounts if n != old):
        return False, f"'{new}' would share files with another wallet (slug '{new_slug}')"

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

    # Move the wallet's strategy + engine-state files to the new slug —
    # a registry-only rename would orphan them (per-wallet split).
    try:
        moves = [(strategies.path_for(old), strategies.path_for(new))]
        for base in (".copied_trades.json", ".position_state.json",
                     ".swing_state.json", ".intraday_state.json"):
            moves.append((strategies.state_path(base, old),
                          strategies.state_path(base, new)))
        for src, dst in moves:
            if os.path.exists(src) and not os.path.exists(dst):
                os.replace(src, dst)
        # Scheduler stamps live INSIDE .trading_schedule_state.json keyed by
        # slug — re-key them too, or the renamed wallet reads "never ran" and
        # its swing/copy loops double-fire the same day.
        sched = HERE / ".trading_schedule_state.json"
        if sched.exists():
            import json
            state = json.loads(sched.read_text())
            old_slug = strategies.slug(old)
            if old_slug in state and new_slug not in state:
                state[new_slug] = state.pop(old_slug)
                sched.write_text(json.dumps(state, indent=2) + "\n")
    except Exception:
        pass                    # a label rename must never hard-fail on moves

    if _current == old:
        _current = new
    if strategies.active() == old:      # keep the strategy context in step —
        strategies.set_active(new)      # its old file was just moved away
    return True, f"renamed '{old}' → '{new}'"


def _probe_account(key: str, secret: str, base: str) -> dict | None:
    """GET /account with explicit creds. Account dict on success, None on
    auth/network failure — the add() gate that keeps bad keys out of .env."""
    import requests
    try:
        r = requests.get(f"{base}/account",
                         headers={"APCA-API-KEY-ID": key,
                                  "APCA-API-SECRET-KEY": secret},
                         timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def add(name: str, key: str, secret: str) -> tuple[bool, str]:
    """Register a NEW wallet end-to-end: validate the keys LIVE against
    /v2/account, append them to .env (env-var names derived from the wallet
    slug), create the wallet's strategy file with every engine switch OFF, and
    add it to the registry LAST — so a failure part-way never leaves a
    half-visible wallet. On success the message carries the live account
    number + equity, so a wrong-account keypair is caught on the spot."""
    import strategies

    name = (name or "").strip()
    key, secret = (key or "").strip(), (secret or "").strip()
    if not name:
        return False, "name cannot be empty"
    if not key or not secret:
        return False, "API key and secret are both required"
    accounts = _load_block()["accounts"]
    if name in accounts:
        return False, f"'{name}' already exists"
    new_slug = strategies.slug(name)
    if not new_slug:
        return False, f"'{name}' needs at least one ascii letter/digit (file slug)"
    if any(strategies.slug(n) == new_slug for n in accounts):
        return False, f"'{name}' would share files with another wallet (slug '{new_slug}')"

    suffix = new_slug.upper()
    key_env, secret_env = f"ALPACA_API_KEY_{suffix}", f"ALPACA_SECRET_KEY_{suffix}"
    env_file = HERE / ".env"
    env_text = env_file.read_text() if env_file.exists() else ""
    for var in (key_env, secret_env):
        if os.getenv(var) or f"{var}=" in env_text:
            return False, f"{var} already set — pick a different wallet name"

    base = (os.getenv("ALPACA_BASE_URL", "") or _DEFAULT_BASE).rstrip("/")
    acct = _probe_account(key, secret, base)
    if acct is None:
        return False, "Alpaca rejected the keys (or network error) — nothing saved"

    # 1. credentials → .env (+ live process env so the wallet works right away)
    nl = "" if (not env_text or env_text.endswith("\n")) else "\n"
    with open(env_file, "a") as f:
        f.write(f"{nl}{key_env}={key}\n{secret_env}={secret}\n")
    os.environ[key_env], os.environ[secret_env] = key, secret

    # 2. strategy file (engines OFF) — template = the default wallet's file.
    #    An existing file (e.g. left by a removed wallet) is kept, not clobbered.
    spath = strategies.path_for(name)
    if not os.path.exists(spath):
        import copy
        import json
        try:
            body = copy.deepcopy(strategies.load_strategy(default_name()))
        except FileNotFoundError:
            body = {}
        for sect, k in (("swing", "enabled"),
                        ("capitol_copier", "autorun_enabled"),
                        ("intraday", "enabled")):
            if sect in body:
                body[sect][k] = False
        body["updated_by"] = f"wallets.add: engines OFF for '{name}'"
        with open(spath, "w") as f:
            json.dump(body, f, indent=2)
            f.write("\n")

    # 3. registry LAST
    import config_io

    def mut(cfg):
        w = cfg.setdefault("wallets", {})
        w.setdefault("accounts", {})[name] = {
            "key_env": key_env, "secret_env": secret_env,
            "base_env": "ALPACA_BASE_URL"}
        w.setdefault("default", next(iter(w["accounts"])))
        return cfg

    try:
        config_io.update_config(mut)
    except Exception as e:
        return False, f"config write failed: {e}"

    num = acct.get("account_number", "?")
    try:
        eq = f"${float(acct.get('equity') or 0):,.0f}"
    except (TypeError, ValueError):
        eq = "?"
    return True, f"account {num} · equity {eq}"


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
