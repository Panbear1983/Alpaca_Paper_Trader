"""
trading_scheduler.py — single heartbeat dispatcher for all autonomous trading,
now PER WALLET.

A launchd job runs this every 10 minutes (same pattern as report_scheduler.py).
It self-gates in ET, then loops over every configured wallet in
strategy_config.json's "wallets" block. For each wallet it:

  1. loads that wallet's OWN strategy file (strategy_<slug>.json)
  2. skips instantly if that wallet's master switches are all off
  3. binds the wallet — strategies.set_active() + wallets.apply() (credential
     rebind) — so an engine can NEVER run wallet A's strategy against wallet
     B's account
  4. dispatches whatever is due:

  EXIT ENGINE   capitol_copier.manage_only()   every `manage_every_minutes`
                during market hours (stop-loss / trail / take-profit / pyramid)
  SWING BUYER   swing_buyer.run()              once per day in the
                swing_time_et window (regime-filtered RS entries + dip-adds)
  COPY LOOP     capitol_copier.run()           once per day in the
                copy_time_et window (politician disclosure copying)
  INTRADAY      intraday_momentum.run_tick()   every `intraday_every_minutes`
                during market hours (4x RS rotation + mandatory EOD flatten —
                WARNING: its flatten closes ALL positions on the account, so
                only enable on a wallet dedicated to intraday)

Master switches live in EACH WALLET'S strategy file (nothing trades for a
wallet until you flip them in the TUI 'n' editor while viewing that wallet):
  swing.enabled                  → gates the swing buyer
  capitol_copier.autorun_enabled → gates BOTH the exit engine and the copy loop
  intraday.enabled               → gates the intraday momentum engine

Idempotency state in .trading_schedule_state.json, keyed per wallet slug
(last manage tick, last swing/copy dates) so restarts never double-fire.

Run manually to test:  python3 trading_scheduler.py
"""
import os
import json
import sys
import datetime as dt
from zoneinfo import ZoneInfo

HERE       = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".trading_schedule_state.json")
ET = ZoneInfo("America/New_York")

_STAMP_KEYS = ("last_manage_at", "last_swing_date", "last_copy_date")


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _migrate_flat_state(state: dict) -> None:
    """Pre-split state was flat; move legacy stamps under the default wallet."""
    if any(k in state for k in _STAMP_KEYS):
        import strategies
        import wallets
        slug = strategies.slug(wallets.default_name())
        legacy = {k: state.pop(k) for k in _STAMP_KEYS if k in state}
        state.setdefault(slug, {}).update(legacy)


def _in_window(now: dt.datetime, hhmm: str, minutes: int) -> bool:
    try:
        h, m = map(int, hhmm.split(":"))
    except Exception:
        return False
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(minutes=minutes)


def _run_wallet(name: str, state: dict, now: dt.datetime, today: str) -> list:
    """Dispatch due engines for ONE wallet. Returns the actions run."""
    import strategies
    import wallets

    tag = f"[trade-sched:{name}]"
    try:
        scfg = strategies.load_strategy(name)
    except FileNotFoundError:
        print(f"{tag} no strategy file — skip")
        return []

    capitol_on  = scfg.get("capitol_copier", {}).get("autorun_enabled", False)
    swing_on    = scfg.get("swing", {}).get("enabled", False)
    intraday_on = scfg.get("intraday", {}).get("enabled", False)
    if not capitol_on and not swing_on and not intraday_on:
        print(f"{tag} engines disabled — skip")
        return []

    ts = scfg.get("trading_schedule", {}) or {}
    if ts.get("weekdays_only", True) and now.weekday() >= 5:
        print(f"{tag} weekend — skip")
        return []

    # Bind this wallet: strategy context + credentials. The cred rebind is the
    # cross-wiring guard — never run engines for a wallet without it.
    strategies.set_active(name)
    ok, msg = wallets.apply(name)
    if not ok:
        print(f"{tag} SKIP — credential bind failed: {msg}", file=sys.stderr)
        return []

    wst = state.setdefault(strategies.slug(name), {})
    ran = []

    # ── 1. Exit engine (high frequency) ────────────────────────────────────
    if capitol_on:
        every = int(ts.get("manage_every_minutes", 20))
        last  = wst.get("last_manage_at")
        due   = True
        if last:
            try:
                last_dt = dt.datetime.fromisoformat(last)
                due = (now - last_dt).total_seconds() >= every * 60 - 30
            except Exception:
                pass
        if due:
            print(f"{tag} {now:%H:%M ET} → exit engine (manage-only)")
            import capitol_copier as cc
            try:
                cc.manage_only(dry_run=False, require_market_open=True)
                wst["last_manage_at"] = now.isoformat()
                ran.append("manage")
            except Exception as e:
                print(f"{tag} manage error: {e}", file=sys.stderr)

    # ── 2. Swing buyer (daily) ──────────────────────────────────────────────
    win = int(ts.get("window_minutes", 20))
    if swing_on and wst.get("last_swing_date") != today and \
            _in_window(now, ts.get("swing_time_et", "10:00"), win):
        print(f"{tag} {now:%H:%M ET} → swing buyer (daily)")
        import swing_buyer
        try:
            swing_buyer.run(dry_run=False)
            wst["last_swing_date"] = today
            ran.append("swing")
        except Exception as e:
            print(f"{tag} swing error: {e}", file=sys.stderr)

    # ── 3. Disclosure copy loop (daily) ─────────────────────────────────────
    if capitol_on and wst.get("last_copy_date") != today and \
            _in_window(now, ts.get("copy_time_et", "10:30"), win):
        print(f"{tag} {now:%H:%M ET} → capitol copy loop (daily)")
        import capitol_copier as cc
        try:
            cc.run(dry_run=False)
            wst["last_copy_date"] = today
            ran.append("copy")
        except Exception as e:
            print(f"{tag} copy error: {e}", file=sys.stderr)

    # ── 4. Intraday momentum (high frequency) ───────────────────────────────
    if intraday_on:
        every = int(ts.get("intraday_every_minutes", 10))
        last  = wst.get("last_intraday_at")
        due   = True
        if last:
            try:
                last_dt = dt.datetime.fromisoformat(last)
                due = (now - last_dt).total_seconds() >= every * 60 - 30
            except Exception:
                pass
        if due:
            print(f"{tag} {now:%H:%M ET} → intraday momentum tick")
            import intraday_momentum as im
            try:
                # load_config() = global ∪ ACTIVE wallet (bound above); run_tick
                # re-checks intraday.enabled and the Alpaca market clock itself.
                im.run_tick(im.load_config(), dry_run=False)
                wst["last_intraday_at"] = now.isoformat()
                ran.append("intraday")
            except Exception as e:
                print(f"{tag} intraday error: {e}", file=sys.stderr)

    return ran


def main() -> int:
    import strategies
    import wallets

    now   = dt.datetime.now(ET)
    today = now.date().isoformat()

    # Regular session only (scheduler-level guard; each engine re-checks too)
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if not (open_t <= now < close_t):
        print(f"[trade-sched] {now:%H:%M ET} outside market hours — skip")
        return 0

    state = _load_state()
    _migrate_flat_state(state)

    ran_any = []
    try:
        for w in wallets.list_wallets():
            if not w["configured"]:
                continue
            actions = _run_wallet(w["name"], state, now, today)
            ran_any += [f"{w['name']}:{a}" for a in actions]
    finally:
        strategies.set_active(None)     # never leak a wallet context

    if ran_any:
        _save_state(state)
        print(f"[trade-sched] done: {', '.join(ran_any)}")
    else:
        _save_state(state)              # persists the flat→per-wallet migration
        print(f"[trade-sched] {now:%H:%M ET} nothing due this tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
