"""
Capitol Trades Copier — pool-aware version
==============================================
Iterates over all politicians in the active pool (managed by pool_manager.py and
selected by politician_vetter.py), scrapes each one's latest disclosed trades,
and copies new buys to Alpaca with per-politician position sizing.

Sizing formula:
  trade_size = daily_budget × pool_weight × consensus_multiplier
  (clamped to [min_position_usd, max_position_usd])

State tracking:
  .copied_trades.json   stores tx_ids that have been copied (dedup across all pool members)

Modes:
  python3 capitol_copier.py              normal trickle-copy run
  python3 capitol_copier.py --rebalance  one-time reallocation toward --target-pct
                                          of equity, spread across each pool member's
                                          most recent distinct disclosed buys
"""

import os, json, re, requests
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv

import pool_manager
from sectors import sector_of
try:
    import sentiment_check
    import telegram_notifier as tg
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False
    tg = None

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = (lambda u: u if not u else (u.rstrip("/") if u.rstrip("/").endswith("/v2")
                                        else u.rstrip("/") + "/v2"))(
                 os.getenv("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets/v2")
DATA_URL   = "https://data.alpaca.markets/v2"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type":        "application/json",
}

CT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "text/x-component",
    "RSC":        "1",
}

import strategies

# Per-wallet paths, resolved at CALL time via strategies.active() — dedup ids
# and trail/pyramid state must never be shared across wallets.
def _state_file() -> str:
    return strategies.state_path(".copied_trades.json")


def _pos_state_file() -> str:
    return strategies.state_path(".position_state.json")

# Symbols never managed by Capitol Copier (legacy / test holdings)
NON_CC_SYMBOLS = ()   # 2026-08-30: TSLA/AAPL exemption removed — the $27.9k TSLA position had no exit engine


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(_state_file()):
        with open(_state_file()) as f:
            return json.load(f)
    return {
        "copied":  [],
        "last_check": None,
        "stats":   {"total_buys": 0, "total_sells": 0},
        "by_politician": {},
    }


def save_state(state):
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    with open(_state_file(), "w") as f:
        json.dump(state, f, indent=2)


def load_config():
    """Global app config ∪ the ACTIVE wallet's strategy file."""
    return strategies.load_merged()


# ── Capitol Trades scraper ───────────────────────────────────────────────────

def fetch_politician_trades(pol_id, per_page=96):
    """Scrape latest trades for a politician from capitoltrades.com RSC stream."""
    url = (f"https://www.capitoltrades.com/politicians/{pol_id}"
           f"?per_page={per_page}&sort=-reportedAt")
    try:
        r = requests.get(url, headers={**CT_HEADERS, "Next-Url": f"/politicians/{pol_id}"},
                         verify=False, timeout=20)
    except Exception as e:
        print(f"  [SCRAPER {pol_id}] Fetch error: {e}")
        return []

    c = r.text
    ticker_list  = re.findall(r'"issuerTicker":"([^"]+)"', c)
    issuer_list  = re.findall(r'"issuerName":"([^"]+)"',   c)
    txid_list    = re.findall(r'"_txId":(\d+)',            c)
    txtype_list  = re.findall(r'"txType":"(buy|sell)"',    c)
    txdate_list  = re.findall(r'"txDate":"(\d{4}-\d{2}-\d{2})"', c)
    pubdate_list = re.findall(r'"pubDate":"(\d{4}-\d{2}-\d{2})', c)
    value_list   = re.findall(r'"value":(\d+)',            c)
    owner_list   = re.findall(r'"owner":"([^"]*)"',        c)
    gap_list     = re.findall(r'"reportingGap":(\d+)',     c)

    n = min(len(ticker_list), len(txtype_list), len(txdate_list))
    trades = []
    for i in range(n):
        raw = ticker_list[i]
        ticker = raw.split(":")[0] if ":" in raw else raw
        if not ticker or ticker in ("null", ""):
            continue
        trades.append({
            "tx_id":         txid_list[i] if i < len(txid_list) else "",
            "ticker":        ticker,
            "issuer":        issuer_list[i] if i < len(issuer_list) else "",
            "tx_type":       txtype_list[i],
            "tx_date":       txdate_list[i],
            "pub_date":      pubdate_list[i] if i < len(pubdate_list) else "",
            "value":         int(value_list[i]) if i < len(value_list) else 0,
            "owner":         owner_list[i] if i < len(owner_list) else "",
            "gap_days":      int(gap_list[i]) if i < len(gap_list) else 0,
            "politician_id": pol_id,  # track source
        })

    return trades


# ── Alpaca helpers ───────────────────────────────────────────────────────────

# ── Per-name concentration cap ───────────────────────────────────────────────
# Every engine's orders funnel through place_market_order(), so this is the one
# place a size limit actually binds. It lives in code, not only in config,
# because strategy_high_risk.json is a file the agent is explicitly allowed to
# rewrite (and wb_dynamic_strategy.py rewrites it every 15 minutes).
#
# Why it exists: on 2026-08-17 the wallet bought seven names at $43,120 each on
# a $91k account — 47% of equity per position — and lost 18.5% the next session.
# Nothing in the system said no.
HARD_MAX_POSITION_PCT = 1.0      # default ceiling for wallets NOT listed in HARD_LIMITS

# Total long exposure, not just per name. The per-name cap alone leaves the door
# open: intraday_momentum sizes at equity * gross_mult / top_n, so 20 names at
# 20% each all pass a 25% per-name check while together being 4x equity. A 7%
# adverse move on that is about -28% — worse than 2026-08-18, just spread wider.
# 2.0 rather than 0.9 because intraday is designed around intraday buying power
# and flattening before the close; 1x would break its premise outright.
HARD_MAX_GROSS_EXPOSURE = 2.0    # default ceiling for wallets NOT listed in HARD_LIMITS

# Per-wallet code ceilings (2026-09-22). These are the un-arguable limits: the
# dashboard and the strategy file may set a wallet LOWER than its row here,
# never higher, and nothing an agent can write to disk changes them. Keyed by
# wallet so the two benchmark wallets (Low Risk, Photonic) keep their old
# limits byte for byte — Peter measures High Risk against them.
#
# High Risk: 25% per name, no borrowing. The August loss was 3.5x leverage
# into seven names at once; a 25%/1.0x box makes that arithmetic impossible.
HARD_LIMITS = {
    "High Risk": {"position_pct": 0.25, "gross": 1.0},
}
_DEFAULT_HARD = {"position_pct": HARD_MAX_POSITION_PCT, "gross": HARD_MAX_GROSS_EXPOSURE}


def _hard():
    """The ceiling row for the ACTIVE wallet, resolved at call time so
    wallets.apply() redirects it exactly as it redirects credentials."""
    try:
        import wallets
        return HARD_LIMITS.get(wallets.current(), _DEFAULT_HARD)
    except Exception:
        return _DEFAULT_HARD


def hard_limits_for(wallet: str) -> dict | None:
    """The code ceilings for `wallet`, or None when it has no row (the TUI
    shows the header line only for wallets that are actually boxed)."""
    return HARD_LIMITS.get(wallet)
_CAP_CACHE = {"t": 0.0, "equity": None, "positions": None}
_CAP_CACHE_TTL = 30               # seconds; keeps the cap off the rate limiter


class OrderRejected(Exception):
    """Raised when an order would breach the concentration cap."""


def _max_position_pct(ceiling=None):
    """Configured cap, clamped to the active wallet's code ceiling.

    `ceiling` is only overridden by _trim_to_caps, which clamps to the OLD
    module-wide figure instead: the per-wallet ceiling binds new buys the
    moment it lands in code, but it must never sell anything by itself. A
    trim happens only when the wallet's own config asks for it (plan step 8).
    """
    if ceiling is None:
        ceiling = _hard()["position_pct"]
    try:
        v = float((load_config().get("risk") or {}).get("max_position_pct", ceiling))
    except Exception:
        v = ceiling
    if v != v or v <= 0:                      # NaN or nonsense
        return ceiling
    return min(v, ceiling)


def _cap_snapshot():
    """(equity, {sym: market_value}) with a short cache."""
    import time
    now = time.time()
    if now - _CAP_CACHE["t"] < _CAP_CACHE_TTL and _CAP_CACHE["equity"] is not None:
        return _CAP_CACHE["equity"], _CAP_CACHE["positions"]
    eq = get_account_equity()
    pos = {p["symbol"].upper(): abs(float(p.get("market_value") or 0))
           for p in get_positions()}
    _CAP_CACHE.update({"t": now, "equity": eq, "positions": pos})
    return eq, pos


def _order_value(ticker, notional, qty):
    """Approximate USD value of a proposed order."""
    if notional:
        return float(notional)
    if not qty:
        return 0.0
    try:
        r = requests.get(f"{DATA_URL}/stocks/{ticker}/trades/latest",
                         headers=ALPACA_HEADERS, timeout=10)
        if r.status_code == 200:
            return float(qty) * float(r.json().get("trade", {}).get("p") or 0)
    except Exception:
        pass
    return 0.0


DEFAULT_GROSS_BY_REGIME = {"bull": 2.00, "neutral": 1.25, "bear": 0.75}


def current_regime():
    """'bull' | 'neutral' | 'bear' | 'unknown'. Cached per session upstream."""
    try:
        import market_context
        return market_context.regime_state().get("state", "unknown")
    except Exception:
        return "unknown"


def _max_gross_exposure(ceiling=None):
    """Configured gross limit, scaled by market regime, clamped to the ceiling
    (`ceiling` overridden only by _trim_to_caps — see _max_position_pct).

    Deleveraging happens on the way INTO a downturn rather than after one. The
    regime can only ever reduce the limit — never raise it past
    HARD_MAX_GROSS_EXPOSURE — and 'unknown' falls back to the unconditional
    figure rather than guessing a direction.

    This blocks new buys only. Nothing here forces a sale, so a regime flip
    never liquidates an existing book.
    """
    if ceiling is None:
        ceiling = _hard()["gross"]
    try:
        risk = load_config().get("risk") or {}
        v = float(risk.get("max_gross_exposure", ceiling))
    except Exception:
        risk, v = {}, ceiling
    if v != v or v <= 0:
        v = ceiling
    base = min(v, ceiling)

    regime = current_regime()
    if regime == "unknown":
        return base
    by_regime = risk.get("gross_by_regime") or DEFAULT_GROSS_BY_REGIME
    try:
        r = float(by_regime.get(regime, base))
    except Exception:
        r = base
    if r != r or r <= 0:
        r = base
    return min(base, r, ceiling)


def check_gross_cap(ticker, side, notional=None, qty=None):
    """(allowed, reason). Total long exposure across the whole book.

    pool.max_total_exposure_pct already exists but is only honoured by
    swing_buyer and capitol_copier — intraday_momentum and isr_alpha, which do
    most of the trading, ignore it. Checking here catches every engine.
    """
    if str(side).lower() != "buy":
        return True, ""
    cap = _max_gross_exposure()
    try:
        equity, positions = _cap_snapshot()
    except Exception as e:
        return False, f"cannot read equity/positions to enforce gross cap ({e})"
    if not equity or equity <= 0:
        return False, "equity unavailable — refusing to size a position blind"
    add = _order_value(ticker, notional, qty)
    if add <= 0:
        return False, f"cannot price the order for {ticker} — refusing"
    gross_now = sum(positions.values())
    after = gross_now + add
    if after > equity * cap:
        return False, (f"gross exposure would reach ${after:,.0f} = "
                       f"{after / equity:.2f}x equity, over the {cap:.2f}x cap "
                       f"(book ${gross_now:,.0f}, adding ${add:,.0f})")
    return True, ""


def check_position_cap(ticker, side, notional=None, qty=None):
    """(allowed, reason). Buys only — selling always reduces exposure."""
    if str(side).lower() != "buy":
        return True, ""
    cap = _max_position_pct()
    try:
        equity, positions = _cap_snapshot()
    except Exception as e:
        return False, f"cannot read equity/positions to enforce cap ({e})"
    if not equity or equity <= 0:
        # Fail closed. An unbounded order is the exact risk this guards against,
        # and a blocked trade is recoverable where a 47% position is not.
        return False, "equity unavailable — refusing to size a position blind"
    add = _order_value(ticker, notional, qty)
    if add <= 0:
        return False, f"cannot price the order for {ticker} — refusing"
    held = positions.get(str(ticker).upper(), 0.0)
    after = held + add
    if after > equity * cap:
        return False, (f"{ticker} would reach ${after:,.0f} = "
                       f"{after / equity * 100:.0f}% of ${equity:,.0f} equity, "
                       f"over the {cap * 100:.0f}% cap "
                       f"(holds ${held:,.0f}, adding ${add:,.0f})")
    return True, ""


# ── Trade attribution ────────────────────────────────────────────────────────
# Five engines trade this account and performance_tracker used to guess which
# one by looking at the ticker, defaulting everything to "capitol_copier". That
# made the profit factor an average across five different strategies with no way
# to separate them. Stamping the source into client_order_id at placement makes
# every fill traceable to the engine that opened it — Alpaca echoes the field
# back on the order, so nothing extra needs fetching.
SOURCE_BY_MODULE = {
    "isr_alpha.executor":    "isr",
    "swing_buyer":           "swing",
    "intraday_momentum":     "intraday",
    "capitol_copier":        "copier",
    "__main__":              "copier",     # capitol_copier run directly
    "price_watcher":         "watcher",
    "inverse_hedge":         "hedge",
    "opportunistic_picker":  "picker",
    "rebalance_top_n":       "rebalance",
    "anchor_trade":          "anchor",
    "tui":                   "manual",
    "manual_trade":          "manual",
    "broker_stops":          "pstop",      # resting trailing-stop guards at the broker
}
KNOWN_SOURCES = set(SOURCE_BY_MODULE.values()) | {"other"}


def _caller_source(depth=2):
    """Which engine is placing this order, from the calling module's name.

    Uses sys._getframe rather than inspect.stack(): the latter builds full
    frame objects for the whole stack and is far too slow to sit on the order
    path. Deriving it here means none of the 25 call sites need to change.

    Any script run directly (`python3 anchor_trade.py ...`) has __name__ ==
    "__main__" in its own globals, same as every other directly-run script —
    so __name__ alone can't tell anchor_trade.py apart from tui.py apart from
    capitol_copier.py itself. Fall back to the file's own name (__file__) in
    that case, which is stable regardless of how the script was launched.
    """
    import sys as _sys
    try:
        g = _sys._getframe(depth).f_globals
    except Exception:
        return "other"
    name = g.get("__name__", "") or ""
    if name == "__main__":
        f = g.get("__file__", "") or ""
        if f:
            name = Path(f).stem
    if name in SOURCE_BY_MODULE:
        return SOURCE_BY_MODULE[name]
    return SOURCE_BY_MODULE.get(name.rsplit(".", 1)[-1], "other")


def _client_order_id(source):
    """`{source}-{YYYYMMDD}-{8 hex}` — under Alpaca's 128-char limit, unique,
    and the source is readable as the first '-' delimited field."""
    import uuid
    return f"{source}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def _log_guardrail(ticker, side, notional, qty, which, why):
    """Record a blocked order so the daily oversight run can see it.

    Without this the block is printed to a scheduler log and thrown away, so
    "the cap stopped three orders today" is unanswerable — the guardrails would
    be invisible in exactly the review meant to judge them.
    """
    try:
        import json as _json
        from datetime import datetime as _dt
        d = _HERE / "diary" if "_HERE" in globals() else Path(__file__).resolve().parent / "diary"
        d.mkdir(parents=True, exist_ok=True)
        row = {"ts": _dt.now().isoformat(timespec="seconds"), "symbol": ticker,
               "side": side, "notional": notional, "qty": qty,
               "cap": which.replace("check_", "").replace("_cap", ""),
               "reason": why, "source": _caller_source(depth=3)}
        with open(d / "guardrail_log.jsonl", "a") as f:
            f.write(_json.dumps(row) + "\n")
    except Exception:
        pass          # never let logging block an order path decision


def _log_manual_trade(ticker, side, notional, qty, order):
    """Record every order Peter places by hand (source == "manual", i.e. through
    the TUI) so his own buying pattern on this wallet has a running record,
    separate from what the automated engines and Wanna Buffet do."""
    try:
        import json as _json
        d = Path(__file__).resolve().parent / "diary"
        d.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "symbol": ticker, "side": side, "notional": notional, "qty": qty,
            "order_id": order.get("id"), "client_order_id": order.get("client_order_id"),
            "status": order.get("status"),
        }
        with open(d / "manual_trades.jsonl", "a") as f:
            f.write(_json.dumps(row) + "\n")
    except Exception:
        pass          # never let logging block an order path decision


def _entry_gate(ticker, side, notional=None, qty=None):
    """The anchor-plan fence (entry_gate.py): universe, entry window, daily-loss
    halt, cooling-off pause, concurrent-name cap, cash floor, entries-per-name,
    consecutive-loss halt. Buy-only, like the caps.
    Fails closed: if the gate itself cannot run, a buy does not go through."""
    try:
        import entry_gate
        return entry_gate.check_entry(ticker, side, notional, qty)
    except Exception as e:
        return False, f"gate: entry gate failed to run ({type(e).__name__}: {e}) — refusing the buy", "gate"


def place_market_order(ticker, side, notional=None, qty=None):
    for _check in (check_position_cap, check_gross_cap, _entry_gate):
        res = _check(ticker, side, notional, qty)
        ok, why = res[0], res[1]
        if not ok:
            which = res[2] if len(res) > 2 and res[2] else _check.__name__
            print(f"[cap] BLOCKED buy {ticker}: {why}")
            _log_guardrail(ticker, side, notional, qty, which, why)
            return {"blocked_by_cap": True, "reason": why, "symbol": ticker}
    source = _caller_source()
    # Boxed wallets keep a resting trailing-stop guard on every position
    # (broker_stops.py). Alpaca refuses a sell while those shares are held by
    # the guard, so every sell — engine, stop, trim, manual — releases it first.
    # The reconciler re-arms whatever remains within a minute. Done here, not
    # in a wrapper: _caller_source() above must keep its frame depth.
    if str(side).lower() == "sell":
        try:
            import broker_stops
            if broker_stops.enabled():
                broker_stops.release(ticker)
                if qty:
                    pos = get_position(ticker) or {}
                    avail = abs(float(pos.get("qty_available") or 0))
                    if avail and float(qty) > avail:
                        qty = round(avail, 4)
        except Exception as e:
            print(f"[stops] release-before-sell failed for {ticker}: {e}")
    payload = {
        "symbol":          ticker,
        "side":            side,
        "type":            "market",
        "time_in_force":   "day",
        "client_order_id": _client_order_id(source),
    }
    if notional:
        payload["notional"] = str(round(notional, 2))
    elif qty:
        payload["qty"] = str(qty)
    r = requests.post(f"{BASE_URL}/orders", headers=ALPACA_HEADERS, json=payload)
    result = r.json()
    if source == "manual" and isinstance(result, dict) and result.get("id"):
        _log_manual_trade(ticker, side, notional, qty, result)
    return result


def get_position(ticker):
    r = requests.get(f"{BASE_URL}/positions/{ticker}", headers=ALPACA_HEADERS)
    return r.json() if r.status_code == 200 else None


def get_account_equity():
    r = requests.get(f"{BASE_URL}/account", headers=ALPACA_HEADERS)
    if r.status_code != 200:
        return None
    return float(r.json().get("equity", 0))


def get_positions():
    """All open Alpaca positions (raw dicts). Empty list on error."""
    r = requests.get(f"{BASE_URL}/positions", headers=ALPACA_HEADERS)
    if r.status_code != 200:
        return []
    return r.json() or []


def get_capitol_exposure():
    """Sum of market value across positions tagged as Capitol Copier
    (everything except the legacy/test holdings in NON_CC_SYMBOLS)."""
    total = 0
    for p in get_positions():
        if p["symbol"] in NON_CC_SYMBOLS:
            continue
        total += abs(float(p.get("market_value", 0)))
    return total


# ── Dynamic position management (stop-loss / trail / take-profit / pyramid) ──

def load_pos_state():
    if os.path.exists(_pos_state_file()):
        with open(_pos_state_file()) as f:
            return json.load(f)
    return {}


def save_pos_state(pstate):
    with open(_pos_state_file(), "w") as f:
        json.dump(pstate, f, indent=2)


def _anchor_names():
    try:
        return {str(x).upper() for x in (load_config().get("anchor") or {}).get("universe") or []}
    except Exception:
        return set()


def core_holds():
    """Symbols Peter marked CORE in the dashboard (anchor.core_holds).

    The five-up-days rule and the take-profit tiers leave these alone so they
    can compound. Caps and the trailing stop still apply — protection is never
    optional, trimming is. Read per call: the file may change under a running
    watcher, and load_config() is mtime-cached anyway.
    """
    try:
        return {str(x).upper() for x in (load_config().get("anchor") or {}).get("core_holds") or []}
    except Exception:
        return set()


def _sellable_qty(p):
    """Shares a software sell may ask for.

    Normally qty_available (Alpaca rejects a sell above it while an open order
    exists). On a boxed wallet the resting guard holds almost every share, so
    qty_available would be the fractional remainder and a cap trim could never
    complete — there the whole position counts, because place_market_order
    releases the guard before it sells.
    """
    q  = abs(float(p.get("qty", 0) or 0))
    qa = abs(float(p.get("qty_available", q) or 0))
    try:
        import broker_stops
        if broker_stops.enabled():
            return q
    except Exception:
        pass
    return qa


def _stop_frac(sym, configured):
    """Stop-loss distance for `sym` as a fraction: the per-name guard width on
    a boxed wallet (so the software backstop agrees with the broker guard),
    the configured dynamic_exits.stop_loss_pct everywhere else."""
    try:
        import broker_stops
        if broker_stops.enabled():
            return broker_stops.width_pct(sym) / 100.0
    except Exception:
        pass
    return configured


def _trim_to_caps(positions, equity, dry_run, acts):
    """Sell down over-cap holdings. Returns the number of sell orders sent.

    1. Any single name above equity x max_position_pct is cut back to the cap.
    2. If the whole book is still above equity x gross cap, the largest
       non-anchor names are cut pro-rata until it is under.
    Shares come from qty_available (Alpaca rejects a sell above it while an
    open order exists) and are rounded to 4 dp like the take-profit path.

    The caps used HERE are the wallet's configured ones (clamped only to the
    old module-wide 1.0 / 2.0), not the per-wallet code ceiling: landing a
    ceiling in code must never liquidate part of the book on its own. Setting
    the config to the ceiling (plan step 8) is what triggers the trims.
    """
    import math
    if not equity or equity <= 0 or not positions:
        return 0
    tag = "[DRY] " if dry_run else ""
    name_cap = equity * _max_position_pct(ceiling=_DEFAULT_HARD["position_pct"])
    sent = 0
    book = {}
    for p in positions:
        sym = p["symbol"]
        mv  = abs(float(p.get("market_value", 0) or 0))
        px  = float(p.get("current_price", 0) or 0)
        avail = _sellable_qty(p)
        book[sym] = {"mv": mv, "px": px, "avail": avail}
        if px <= 0 or avail <= 0:
            continue
        if mv > name_cap * 1.005:                     # 0.5% tolerance: no dust trades
            excess = mv - name_cap
            qty = min(avail, round(excess / px, 4))
            if qty <= 0:
                continue
            print(f"  {tag}✂ TRIM-CAP(name) {sym}  ${mv:,.0f} = {mv/equity*100:.0f}% of equity, "
                  f"cap {name_cap/equity*100:.0f}%  → sell {qty:g} (${qty*px:,.0f})")
            acts.append(f"✂ TRIM `{sym}` ${qty*px:,.0f} (over {name_cap/equity*100:.0f}% cap)")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
                _log_guardrail(sym, "sell", None, qty, "trim",
                           f"{sym} at {mv/equity*100:.0f}% of equity, over the {name_cap/equity*100:.0f}% cap — sold ${qty*px:,.0f}")
            book[sym]["mv"] -= qty * px
            book[sym]["avail"] -= qty
            sent += 1
    gross_cap = equity * _max_gross_exposure(ceiling=_DEFAULT_HARD["gross"])
    gross = sum(b["mv"] for b in book.values())
    if gross > gross_cap * 1.005:
        anchors = _anchor_names()
        excess = gross - gross_cap
        # Largest non-anchor names first, each cut as far as needed — one or two
        # orders rather than a dozen dust sells across the whole book.
        pool = sorted(((s_, b) for s_, b in book.items()
                       if s_ not in anchors and b["px"] > 0 and b["avail"] > 0),
                      key=lambda kv: -kv[1]["mv"])
        for sym, b in pool:
            if excess <= 50:
                break
            cut = min(b["mv"], excess)
            qty = min(b["avail"], round(cut / b["px"], 4))
            if qty <= 0 or qty * b["px"] < 50:
                continue
            excess -= qty * b["px"]
            print(f"  {tag}✂ TRIM-CAP(gross) {sym}  book ${gross:,.0f} = {gross/equity:.2f}x equity, "
                  f"cap {gross_cap/equity:.2f}x  → sell {qty:g} (${qty*b['px']:,.0f})")
            acts.append(f"✂ TRIM `{sym}` ${qty*b['px']:,.0f} (book over {gross_cap/equity:.2f}x)")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
                _log_guardrail(sym, "sell", None, qty, "trim",
                           f"book at {gross/equity:.2f}x equity, over the {gross_cap/equity:.2f}x cap — sold ${qty*b['px']:,.0f} of {sym}")
            sent += 1
    return sent


def manage_open_positions(cfg, dry_run=False):
    """Active management for every Capitol Copier position, evaluated each run.

    Per position, in priority order (at most one action per position per run):
      1. Stop-loss   — unrealized loss <= -stop_loss_pct  → sell all.
      2. Trail stop  — once peak gain >= trail_trigger_pct, if price falls
                       trail_giveback_pct below the peak → sell all.
      3. Take-profit — at each gain tier in take_profit_levels, sell that
                       fraction of the remaining qty (one tier per run).
      4. Pyramid     — at each gain tier in pyramid_levels, add
                       pyramid_add_frac x original size, if the exposure cap allows.

    Peak price, pyramid count and take-profit stage are persisted in
    .position_state.json so tiers fire only once.
    """
    dx = cfg.get("dynamic_exits", {})
    if not dx.get("enabled", False):
        return

    stop_loss      = dx.get("stop_loss_pct", 0.08)
    trail_trigger  = dx.get("trail_trigger_pct", 0.15)
    trail_giveback = dx.get("trail_giveback_pct", 0.08)
    tp_levels      = dx.get("take_profit_levels", [])
    pyr_levels     = dx.get("pyramid_levels", [])
    pyr_frac       = dx.get("pyramid_add_frac", 0.5)
    prune_off      = dx.get("prune_off_target", False)
    max_hold       = dx.get("max_holdings", 0)

    positions = [p for p in get_positions() if p["symbol"] not in NON_CC_SYMBOLS]
    if not positions:
        print("  [manage] no Capitol Copier positions to manage.")
        return

    pstate   = load_pos_state()
    equity   = get_account_equity() or 100000
    exposure = get_capitol_exposure()
    max_exp  = equity * cfg["pool"]["max_total_exposure_pct"]
    tag      = "[DRY] " if dry_run else ""
    actions  = 0
    acts     = []   # collect all actions, push ONE consolidated message at end

    # prune state for positions we no longer hold (keys starting with "_" are
    # meta-state — e.g. "_stopped" history used by swing_buyer's cooldown)
    held = {p["symbol"] for p in positions}
    for sym in list(pstate.keys()):
        if sym not in held and not sym.startswith("_"):
            pstate.pop(sym, None)

    def _liquidate(p, reason):
        nonlocal actions
        sym = p["symbol"]
        qty = abs(float(p.get("qty", 0)))
        mv  = abs(float(p.get("market_value", 0)))
        print(f"  {tag}✗ {reason} {sym}  → sell all {qty:g} (${mv:,.0f})")
        acts.append(f"🔴 SELL `{sym}` ${mv:,.0f} ({reason})")
        if not dry_run and qty > 0:
            place_market_order(sym, "sell", qty=qty)
        pstate.pop(sym, None)
        actions += 1

    # ── Consolidation pass 0: trim anything over the caps ───────────────────
    # The caps at check_position_cap / check_gross_cap refuse NEW buys only, so a
    # book that is already over them stays over them forever — on 2026-08-29
    # TTAN, TSLA and TSM sat near 40% of equity each and the book at 2.34x while
    # 16 buys were refused and nothing sold. This pass sells the excess every
    # tick. Note the gross cap is regime-scaled (_max_gross_exposure), so a
    # bull->bear flip now forces sales; the docstring above that function used
    # to promise the opposite, and Peter chose this behaviour on 2026-08-30.
    trimmed = _trim_to_caps(positions, equity, dry_run, acts)
    if trimmed:
        actions += trimmed
        _CAP_CACHE["t"] = 0.0          # the next cap check must see the smaller book
        positions = [p for p in get_positions() if p["symbol"] not in NON_CC_SYMBOLS] \
            if not dry_run else positions

    # ── Consolidation pass 1: sweep off-target-sector holdings ──────────────
    # The whitelist blocks new off-target buys; this sheds the legacy ones so
    # the book collapses to the target sectors instead of letting them ride.
    if prune_off:
        survivors = []
        for p in positions:
            if is_eligible_ticker(p["symbol"], cfg):
                survivors.append(p)
            else:
                _liquidate(p, "PRUNE-OFFTARGET")
        positions = survivors

    # ── Consolidation pass 2: hard cap on number of holdings ────────────────
    # Keep the largest `max_hold` on-target positions; sell the long tail.
    if max_hold and len(positions) > max_hold:
        positions.sort(key=lambda p: abs(float(p.get("market_value", 0))), reverse=True)
        for p in positions[max_hold:]:
            _liquidate(p, f"CAP-TAIL(>{max_hold})")
        positions = positions[:max_hold]

    for p in positions:
        sym   = p["symbol"]
        qty   = abs(float(p.get("qty", 0)))
        cur   = float(p.get("current_price", 0) or 0)
        entry = float(p.get("avg_entry_price", 0) or 0)
        plpc  = float(p.get("unrealized_plpc", 0) or 0)   # decimal, +0.12 = +12%
        cost  = abs(float(p.get("cost_basis", 0) or 0))
        if cur <= 0 or entry <= 0 or qty <= 0:
            continue

        st = pstate.setdefault(sym, {
            "entry_price": entry,
            "peak_price":  cur,
            "adds_done":   0,
            "tp_stage":    0,
            "orig_size":   cost or abs(float(p.get("market_value", 0))),
        })
        st["peak_price"] = max(float(st.get("peak_price", cur)), cur)
        peak      = st["peak_price"]
        peak_gain = (peak - entry) / entry if entry else 0.0

        # 1. Stop-loss. On a boxed wallet the broker guard normally fires
        #    first; this is the backstop for the fractional remainder and for
        #    any name whose guard is missing, at the same per-name width.
        stop_here = _stop_frac(sym, stop_loss)
        if plpc <= -stop_here:
            print(f"  {tag}✗ STOP-LOSS {sym}  {plpc*100:+.1f}% <= -{stop_here*100:.1f}%  → sell all {qty:g}")
            acts.append(f"🛑 STOP `{sym}` {plpc*100:+.1f}%")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
                # record stop date so swing_buyer won't rebuy during cooldown
                pstate.setdefault("_stopped", {})[sym] = datetime.now(timezone.utc).date().isoformat()
            pstate.pop(sym, None)
            actions += 1
            continue

        # 2. Trailing stop
        if peak_gain >= trail_trigger and cur <= peak * (1 - trail_giveback):
            print(f"  {tag}✗ TRAIL-STOP {sym}  peak +{peak_gain*100:.0f}%, now {plpc*100:+.1f}% "
                  f"({(cur/peak-1)*100:+.1f}% off peak)  → sell all {qty:g}")
            acts.append(f"📉 TRAIL `{sym}` +{peak_gain*100:.0f}%→{plpc*100:+.1f}%")
            if not dry_run:
                place_market_order(sym, "sell", qty=qty)
                pstate.setdefault("_stopped", {})[sym] = datetime.now(timezone.utc).date().isoformat()
            pstate.pop(sym, None)
            actions += 1
            continue

        # 3. Take-profit (one tier per run). CORE holds are exempt — Peter
        #    marks them to be left to compound (2026-09-22); stops still apply.
        sold_tp = False
        for idx, level in enumerate([] if sym in core_holds() else tp_levels):
            thr, frac = level[0], level[1]
            if st["tp_stage"] <= idx and plpc >= thr:
                sell_qty = round(qty * frac, 4)
                if sell_qty <= 0:
                    continue
                print(f"  {tag}↓ TAKE-PROFIT {sym}  +{plpc*100:.0f}% >= +{thr*100:.0f}%  "
                      f"→ trim {frac*100:.0f}% ({sell_qty:g} sh)")
                acts.append(f"💰 TAKE-PROFIT `{sym}` +{plpc*100:.0f}% trim {frac*100:.0f}%")
                if not dry_run:
                    place_market_order(sym, "sell", qty=sell_qty)
                st["tp_stage"] = idx + 1
                actions += 1
                sold_tp = True
                break
        if sold_tp:
            continue

        # 4. Pyramid into winners (one tier per run, respect exposure cap).
        #    Only compound ON-TARGET names — never add to off-sector legacy
        #    holdings the new regime wouldn't buy (let those bleed down via
        #    take-profits / stops instead).
        if not is_eligible_ticker(sym, cfg):
            continue
        for idx, thr in enumerate(pyr_levels):
            if st["adds_done"] <= idx and plpc >= thr:
                add_size = float(st["orig_size"]) * pyr_frac
                if exposure + add_size > max_exp:
                    print(f"  {tag}• PYRAMID {sym} skipped — would breach exposure cap "
                          f"(${exposure:.0f}+${add_size:.0f} > ${max_exp:.0f})")
                    # do NOT burn the tier — retry when exits free up headroom
                    break
                print(f"  {tag}↑ PYRAMID {sym}  +{plpc*100:.0f}% >= +{thr*100:.0f}%  "
                      f"→ add ${add_size:.0f}")
                acts.append(f"🟢 PYRAMID `{sym}` +${add_size:,.0f}")
                if not dry_run:
                    place_market_order(sym, "buy", notional=add_size)
                st["adds_done"] = idx + 1
                exposure += add_size
                actions += 1
                break

    if actions == 0:
        print("  [manage] no exit/pyramid triggers fired this run.")
    # ── ONE consolidated push for the whole management cycle ─────────────────
    if acts and tg and not dry_run:
        tg.notify_batch("Capitol position management", acts, emoji="🏛")
    if not dry_run:
        save_pos_state(pstate)


# ── Copy logic ───────────────────────────────────────────────────────────────

def is_eligible_ticker(ticker, cfg=None):
    if not ticker or "/" in ticker or len(ticker) > 5:
        return False
    if ticker in ("XSP", "SPX", "VIX", "NDX"):  # known index symbols
        return False
    if ticker == "TSLA":  # TSLA Ladder strategy retired; never re-enter via Capitol Copier
        return False
    if any(c.isdigit() for c in ticker):  # foreign listings, e.g. Bovespa "AZZA3"
        return False
    if len(ticker) == 5 and ticker.endswith("X"):  # 5-letter mutual/money-market funds, e.g. "VMFXX"
        return False
    # Sector-concentration whitelist: only copy names in the target sectors.
    # Tickers with an unknown sector are skipped (keeps the book concentrated).
    if cfg is None:
        cfg = load_config()
    target = cfg["capitol_copier"].get("target_sectors")
    if target:
        if sector_of(ticker) not in target:
            return False
    return True


def copy_trade(trade, all_pool_buys, state, cfg, mode="trade", preview=False):
    """mode "notify" (2026-09-23): run every check, then record an IDEA instead
    of an order; the disclosure is marked handled so it is suggested once.
    `preview`: the notify path with nothing written and the fence judged as if
    inside the window."""
    ticker        = trade["ticker"]
    tx_type       = trade["tx_type"]
    tx_id         = trade["tx_id"]
    politician_id = trade["politician_id"]
    who           = trade.get("politician_name") or politician_id

    if tx_id in state["copied"]:
        return False, "already copied", 0

    if not is_eligible_ticker(ticker, cfg):
        return False, f"off-target/non-standard ticker {ticker}", 0

    # Freshness filter — a disclosure published more than max_disclosure_lag_days
    # ago is stale information; the 30d-hold alpha measured by backtest_engine
    # decays fast with entry lag. (Config key existed but was never enforced.)
    max_lag = cfg["capitol_copier"].get("max_disclosure_lag_days")
    if max_lag:
        pub = trade.get("pub_date", "")
        if pub:
            try:
                age = (date.today() - datetime.strptime(pub, "%Y-%m-%d").date()).days
                if age > max_lag:
                    return False, f"disclosure too old ({age}d > {max_lag}d)", 0
            except ValueError:
                pass

    # SELL logic: only sell if we hold a position
    if tx_type == "sell":
        pos = get_position(ticker)
        if not pos or "symbol" not in pos:
            return False, f"no position in {ticker} to sell", 0
        qty = pos.get("qty", "0")
        if mode == "notify":
            import suggestions
            if not preview:
                suggestions.record("copier", ticker, "sell", 0, f"{who} sold", True, "")
                state["copied"].append(tx_id)
            return False, f"IDEA sell — {who} sold; you hold {float(qty):g} sh", 0
        result = place_market_order(ticker, "sell", qty=qty)
        order_id = result.get("id", "")
        status   = result.get("status", result.get("message", "unknown"))
        if order_id:
            state["copied"].append(tx_id)
            state["stats"]["total_sells"] += 1
            state.setdefault("by_politician", {}).setdefault(politician_id, {"buys":0,"sells":0})["sells"] += 1
            return True, f"sell {order_id[:8]} qty={qty}", 0
        return False, f"sell failed: {status}", 0

    # BUY logic: size by pool weight × consensus boost × sentiment multiplier
    consensus = pool_manager.detect_consensus(ticker, all_pool_buys)
    boost     = consensus["multiplier"]

    # Sentiment overlay (Hermes/gemma local LLM)
    sentiment = None
    sentiment_mult = 1.0
    if SENTIMENT_AVAILABLE:
        try:
            sentiment = sentiment_check.get_sentiment(ticker)
            sentiment_mult = sentiment.get("multiplier", 1.0)
        except Exception as e:
            print(f"  [sentiment] {ticker} check failed: {e}")

    combined_mult = boost * sentiment_mult
    size = pool_manager.get_trade_size(politician_id, sector_multiplier=combined_mult)

    if size <= 0:
        return False, "not in active pool", 0

    # Hard skip if sentiment is very bearish (score 1) — only when the veto is
    # enabled. The aggressive regime disables it (sentiment still scales size).
    if cfg["capitol_copier"].get("sentiment_veto_enabled", True):
        if sentiment and sentiment.get("score") == 1:
            return False, f"sentiment veto (1/5: {sentiment.get('flag','')})", 0

    # Holding-aware sizing — respect the per-name cap against what we ALREADY
    # hold, not just this order's size. Prevents a second disclosure for a name
    # we're full on from doubling exposure.
    max_pos = cfg["pool"]["max_position_usd"]
    pos = get_position(ticker)
    held_mv = abs(float(pos.get("market_value", 0) or 0)) if pos and "symbol" in pos else 0.0
    if held_mv >= max_pos:
        return False, f"already at per-name cap (${held_mv:.0f}/${max_pos})", 0
    if held_mv + size > max_pos:
        size = round(max_pos - held_mv, 2)
        if size < cfg["pool"]["min_position_usd"]:
            return False, f"headroom under per-name cap too small (${size:.0f})", 0

    # Exposure cap check
    equity = get_account_equity() or 100000
    exposure = get_capitol_exposure()
    max_exposure = equity * cfg["pool"]["max_total_exposure_pct"]
    if exposure + size > max_exposure:
        return False, f"would breach {cfg['pool']['max_total_exposure_pct']*100:.0f}% exposure cap (${exposure:.0f}/${max_exposure:.0f})", 0

    if mode == "notify":
        import suggestions
        ok, why = (suggestions.preview_fence if preview else suggestions.fence_check)(ticker, "buy", size)
        note = f"{who} bought" + (f", {consensus['n_members']} pool members agree" if consensus["is_consensus"] else "")
        if not preview:
            suggestions.record("copier", ticker, "buy", size, note, ok, why)
            state["copied"].append(tx_id)
        return False, f"IDEA buy ${size:.0f} — {note} · {'allowed' if ok else 'blocked: ' + why}", size

    result = place_market_order(ticker, "buy", notional=size)
    order_id = result.get("id", "")
    status   = result.get("status", result.get("message", "unknown"))

    if order_id:
        state["copied"].append(tx_id)
        state["stats"]["total_buys"] += 1
        state.setdefault("by_politician", {}).setdefault(politician_id, {"buys":0,"sells":0})["buys"] += 1
        consensus_note = f" [CONSENSUS x{boost} from {consensus['n_members']} members]" if consensus["is_consensus"] else ""
        sentiment_note = ""
        if sentiment:
            sentiment_note = f" [sentiment {sentiment['score']}/5 x{sentiment_mult:.2f}]"
            if sentiment.get("flag"):
                sentiment_note += f" ⚠ {sentiment['flag']}"
        return True, f"buy {order_id[:8]} ${size:.0f}{consensus_note}{sentiment_note}", size
    return False, f"buy failed: {status}", 0


# ── Main ─────────────────────────────────────────────────────────────────────

def run(dry_run=False, preview=False, preview_days=None):
    """`preview` (2026-09-23): the advice path on a scratch copy of the state —
    every disclosure of the last max_disclosure_lag_days is shown as it would
    be suggested, nothing is written, no exits are run, and the text goes to
    Telegram marked TEST."""
    import urllib3
    urllib3.disable_warnings()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    cfg   = load_config()

    if preview:
        state = {**state, "copied": []}                 # scratch: show recent disclosures again
        if preview_days:                                 # a wider window for the demo only
            cfg = {**cfg, "capitol_copier": {**cfg["capitol_copier"], "max_disclosure_lag_days": int(preview_days)}}
        print(f"[{now}] Capitol Copier — PREVIEW (advice only, nothing written)")
    else:
        # 1. Active management of existing positions runs every tick, before any new
        #    copying — and regardless of pool state. In dry-run we ONLY preview this
        #    (no orders, no new copies).
        print(f"[{now}] Capitol Copier — dynamic position management"
              f"{' (DRY RUN)' if dry_run else ''}")
        manage_open_positions(cfg, dry_run=dry_run)
        print()

    if dry_run:
        print("  DRY RUN — skipping new-trade copy loop.")
        return

    pool = pool_manager.get_pool()
    if not pool:
        print(f"[{now}] Capitol Copier — POOL IS EMPTY")
        print("  Run `python3 politician_vetter.py` to populate the pool first.")
        return

    print(f"[{now}] Capitol Copier — pool-aware scan")
    print(f"  Active pool: {len(pool)} members  |  daily budget: ${cfg['pool']['daily_budget_usd']}")
    for p in pool:
        size = pool_manager.get_trade_size(p["politician_id"])
        flag = " [PROBATION]" if p.get("is_probationary") else ""
        print(f"    #{p['rank']} {p['politician_id']}  weight={p['weight']*100:.0f}%  size=${size:.0f}{flag}")
    print(f"  Previously copied: {len(state['copied'])} trades")
    print()

    # Fetch all pool members' trades first (needed for consensus detection)
    all_trades = []
    for member in pool:
        pid = member["politician_id"]
        member_trades = fetch_politician_trades(pid)
        all_trades.extend(member_trades)
        print(f"  Fetched {len(member_trades)} trades for {pid}")

    # Build pool_buys list for consensus detection (only buys, within window)
    all_pool_buys = [t for t in all_trades if t["tx_type"] == "buy"]

    print(f"\n  Processing {len(all_trades)} total trades from pool...\n")

    new_buys, new_sells, skipped = 0, 0, 0
    total_deployed = 0
    acts = []   # collect all copied trades, push ONE consolidated message at end
    import suggestions
    mode = "notify" if preview else suggestions.mode_for(cfg.get("capitol_copier", {}), "copy_mode", "copy_on")
    idea_lines = []

    for t in all_trades:
        if t["tx_id"] in state["copied"]:
            continue

        copied, reason, size = copy_trade(t, all_pool_buys, state, cfg, mode=mode, preview=preview)
        pid = t["politician_id"]
        lag = t.get("gap_days", "?")
        disclosed = t.get("pub_date", "?")
        symbol = f"{t['tx_type'].upper():4s} {t['ticker']:6s}"

        if copied:
            if t["tx_type"] == "buy":
                new_buys += 1
                total_deployed += size
                acts.append(f"🟢 BUY `{t['ticker']}` ${size:,.0f}")
            else:
                new_sells += 1
                acts.append(f"🔴 SELL `{t['ticker']}`")
            print(f"  ✓ COPIED  [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")
        elif reason.startswith("IDEA"):
            idea_lines.append(f"💡 {t['tx_type'].upper()} `{t['ticker']}` {reason[5:].strip()}")
            print(f"  💡 IDEA    [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason[5:].strip()}")
        else:
            skipped += 1
            print(f"  ✗ SKIP    [{pid}] {symbol}  disclosed={disclosed}  lag={lag}d  → {reason}")

    if new_buys == 0 and new_sells == 0 and not idea_lines:
        print("\n  No new trades to copy since last run.")

    # ── ONE consolidated push for all newly-copied trades (or ideas) ─────────
    if preview:
        head = ["🧪 *TEST — Capitol Copier ideas, advice only, nothing ordered*",
                "_How the daily note will look: every disclosure of the last "
                f"{cfg['capitol_copier'].get('max_disclosure_lag_days', 10)} days from the {len(pool)} followed "
                "politicians, as it would be suggested. Sent on request, not by the schedule._", ""]
        body = idea_lines or ["_No fresh disclosure passed the filters in that window._"]
        text = "\n".join(head + body)
        print("\n" + text)
        if tg:
            print("\ntelegram:", "sent" if tg.send(text) else "NOT sent")
        return
    if acts and tg:
        tg.notify_batch("Capitol Copier · new trades", acts, emoji="🏛")
    if idea_lines and tg:
        tg.notify_batch("Capitol Copier · ideas (advice only)", idea_lines, emoji="💡")

    print(f"\n  Session: +{new_buys} buys, +{new_sells} sells, {skipped} skips, "
          f"${total_deployed:.0f} deployed")
    print(f"  Lifetime totals: buys={state['stats']['total_buys']} sells={state['stats']['total_sells']}")

    save_state(state)


def manage_only(dry_run=False, require_market_open=True):
    """Run ONLY the dynamic exit/pyramid engine — no disclosure scan, no copies.
    Designed for a high-frequency schedule (every ~20 min during market hours);
    the copy loop runs separately on its own daily cadence."""
    import urllib3
    urllib3.disable_warnings()

    if require_market_open and not dry_run:
        try:
            r = requests.get(f"{BASE_URL}/clock", headers=ALPACA_HEADERS, timeout=10)
            if not r.json().get("is_open"):
                print("  market CLOSED — manage-only tick skipped.")
                return
        except Exception as e:
            print(f"  clock check failed ({e}) — proceeding anyway.")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] Capitol Copier — manage-only tick{' (DRY RUN)' if dry_run else ''}")
    manage_open_positions(load_config(), dry_run=dry_run)


def sync_state():
    """Reconciliation: mark every currently-visible pool disclosure as copied
    WITHOUT trading, so the next live copy run starts from a clean slate and
    can never re-buy positions that were opened by another process (the old
    cloud routines never wrote to .copied_trades.json)."""
    import urllib3
    urllib3.disable_warnings()

    state = load_state()
    pool  = pool_manager.get_pool()
    if not pool:
        print("POOL IS EMPTY — nothing to sync.")
        return

    before = len(state["copied"])
    for member in pool:
        pid = member["politician_id"]
        trades = fetch_politician_trades(pid)
        added = 0
        for t in trades:
            if t["tx_id"] and t["tx_id"] not in state["copied"]:
                state["copied"].append(t["tx_id"])
                added += 1
        print(f"  {pid}: {len(trades)} disclosures visible, {added} marked as copied")
    save_state(state)
    print(f"\n  Sync complete: {before} → {len(state['copied'])} tx_ids marked copied. "
          f"No orders placed.")


def rebalance(target_pct=0.60, max_tickers_per_member=5):
    """One-time reallocation toward `target_pct` of equity invested via Capitol
    Copier picks, spread across each pool member's most recent distinct buys."""
    import urllib3
    urllib3.disable_warnings()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    cfg   = load_config()
    pool  = pool_manager.get_pool()

    if not pool:
        print(f"[{now}] Capitol Copier REBALANCE — POOL IS EMPTY")
        return

    equity   = get_account_equity() or 0
    exposure = get_capitol_exposure()
    target   = equity * target_pct
    gap      = target - exposure

    print(f"[{now}] Capitol Copier — REBALANCE (target {target_pct*100:.0f}% of equity)")
    print(f"  Equity:           ${equity:,.2f}")
    print(f"  Current exposure: ${exposure:,.2f}")
    print(f"  Target:           ${target:,.2f}")
    print(f"  Gap to deploy:    ${gap:,.2f}")
    print()

    if gap <= 0:
        print("  Already at or above target — nothing to deploy.")
        return

    min_pos    = cfg["pool"]["min_position_usd"]
    max_pos    = cfg["pool"]["max_position_usd"]
    weight_sum = sum(m["weight"] for m in pool) or 1.0

    total_deployed = 0
    n_orders = 0

    for member in pool:
        pid    = member["politician_id"]
        weight = member["weight"] / weight_sum
        bucket = gap * weight
        print(f"  [{pid}] weight={weight*100:.0f}% (normalized)  bucket=${bucket:,.2f}")

        if bucket < min_pos:
            print(f"    -> below ${min_pos} minimum, skipping")
            continue

        trades = fetch_politician_trades(pid)
        buys = [t for t in trades
                if t["tx_type"] == "buy" and is_eligible_ticker(t["ticker"], cfg)]
        buys.sort(key=lambda t: (t.get("pub_date", ""), t.get("tx_date", "")), reverse=True)

        seen, picks = set(), []
        for t in buys:
            if t["ticker"] in seen:
                continue
            seen.add(t["ticker"])
            picks.append(t)
            if len(picks) >= max_tickers_per_member:
                break

        if not picks:
            print("    -> no eligible recent buys found, bucket undeployed")
            continue

        per_ticker = max(min_pos, min(max_pos, bucket / len(picks)))
        print(f"    -> {len(picks)} ticker(s) @ ${per_ticker:,.2f} each: "
              f"{', '.join(t['ticker'] for t in picks)}")

        for t in picks:
            ticker = t["ticker"]
            result = place_market_order(ticker, "buy", notional=per_ticker)
            order_id = result.get("id", "")
            status   = result.get("status", result.get("message", "unknown"))
            if order_id:
                if t["tx_id"] not in state["copied"]:
                    state["copied"].append(t["tx_id"])
                state["stats"]["total_buys"] += 1
                state.setdefault("by_politician", {}).setdefault(pid, {"buys": 0, "sells": 0})["buys"] += 1
                total_deployed += per_ticker
                n_orders += 1
                print(f"      ✓ BUY {ticker:<6} ${per_ticker:,.2f}  order {order_id[:8]}")
            else:
                print(f"      ✗ BUY {ticker:<6} failed: {status}")

    print()
    print(f"  Deployed ${total_deployed:,.2f} across {n_orders} orders")
    new_exposure = exposure + total_deployed
    if equity:
        print(f"  New exposure (est): ${new_exposure:,.2f}  ({new_exposure/equity*100:.1f}% of equity)")

    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebalance", action="store_true",
                         help="One-time reallocation toward target equity allocation via Capitol Copier picks")
    parser.add_argument("--target-pct", type=float, default=0.60,
                         help="Target fraction of equity actively invested (default 0.60)")
    parser.add_argument("--preview", action="store_true",
                        help="advice mode on today's disclosures: build the note, text it as a TEST, write nothing")
    parser.add_argument("--preview-days", type=int, default=None,
                        help="with --preview: look this many days back instead of max_disclosure_lag_days")
    parser.add_argument("--dry-run", action="store_true",
                         help="Preview dynamic-exit/pyramid decisions only — no orders, no new copies")
    parser.add_argument("--manage-only", action="store_true",
                         help="Run the exit/pyramid engine only (no disclosure scan) — for the high-frequency schedule")
    parser.add_argument("--sync-state", action="store_true",
                         help="Mark all currently-visible pool disclosures as copied WITHOUT trading (reconciliation)")
    args = parser.parse_args()

    if args.sync_state:
        sync_state()
    elif args.rebalance:
        rebalance(target_pct=args.target_pct)
    elif args.manage_only:
        manage_only(dry_run=args.dry_run)
    elif args.preview:
        run(preview=True, preview_days=args.preview_days)
    else:
        run(dry_run=args.dry_run)
