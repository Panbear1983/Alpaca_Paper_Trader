#!/usr/bin/env python3
"""
robinhood_view.py — a READ-ONLY view of Peter's Robinhood main account
=======================================================================
Peter's sign-off, 2026-10-13: show the Robinhood MAIN account on the
dashboard, view-only. The Roth IRA and the "Agentic" account are left out.
Trading is a later stage with its own sign-off.

How the numbers get here
  Robinhood does not hand out API keys. Its data is reachable only through
  the Robinhood connector attached to Peter's Claude account. So a reader
  starts a short, headless Claude Code run (cheapest model), lets it call
  five Robinhood READ tools, and keeps Robinhood's own answers — the raw tool
  results, never the model's retyping of them. Every place/cancel/review tool
  is blocked on the command line, built-in tools are switched off, and the
  permission mode refuses anything not pre-approved.

  The headless run loads no user settings (so Peter's read-aloud and
  memory-saving hooks stay silent) and saves no chat session.

What it writes (private, outside the repo, never pushed):
  ~/.robinhood_view/snapshot.json   latest good read
  ~/.robinhood_view/history.jsonl   one account-value row per good read (chart)
  ~/.robinhood_view/status.json     last attempt / last error / failure streak

This is deliberately NOT a wallet in strategy_config.json: that registry
drives the trading scheduler and the reports, which would try to trade it.
Only the dashboard knows this view exists.

    venv/bin/python robinhood_view.py read     one read now, print a summary
    venv/bin/python robinhood_view.py show     print the saved snapshot
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "strategy_config.json"
DATA_DIR = Path(os.environ.get("ROBINHOOD_VIEW_DIR") or (Path.home() / ".robinhood_view"))

PFX = "mcp__claude_ai_Robinhood__"
READ_TOOLS = tuple(PFX + n for n in (
    "get_accounts", "get_portfolio", "get_equity_positions",
    "get_equity_quotes", "get_equity_historicals"))
# Belt and braces: the permission mode already refuses anything not in
# READ_TOOLS, but every tool that can move money or change the account is
# also named here so a settings change can never quietly allow it.
BLOCKED_TOOLS = tuple(PFX + n for n in (
    "place_equity_order", "place_option_order", "place_crypto_order",
    "review_equity_order", "review_option_order", "preview_crypto_order",
    "cancel_equity_order", "cancel_option_order", "cancel_crypto_order",
    "exercise_option", "cancel_option_exercise",
    "create_alert", "update_alert", "delete_alert", "mark_alerts_read",
    "create_watchlist", "update_watchlist", "add_to_watchlist",
    "remove_from_watchlist", "add_option_to_watchlist",
    "remove_option_from_watchlist", "follow_watchlist", "unfollow_watchlist",
    "create_scan", "update_scan_config", "update_scan_filters"))

DEFAULTS = {
    "enabled": True,
    "read_every_minutes": 5,       # while the market is open
    "closed_every_hours": 12,      # while it is closed (plus one read right after the close)
    "stale_warn_minutes": 15,      # dashboard warns when market is open and data is older
    "model": "haiku",
    "timeout_seconds": 150,
}

PROMPT = """You are a read-only data fetcher for a dashboard. Use only these Robinhood tools, in this order:
1. get_accounts.
2. Pick the ONE account whose is_default is true. Use its account_number in steps 3 and 4. Ignore every other account.
3. get_portfolio for that account.
4. get_equity_positions for that account. If the response has a non-empty "next" cursor, call it again with that cursor until there is none.
5. If there are positions: get_equity_quotes with all the position symbols (at most 20 symbols per call; split into several calls if needed).
6. If there are positions: get_equity_historicals with the same symbols (at most 10 symbols per call), interval "day", start_time "{start}".
Never place, review, cancel or change anything. Do not call any other tool. When finished, reply with exactly: done"""


class ReaderError(Exception):
    """A read that must not replace the last good snapshot."""


# ── settings + files ─────────────────────────────────────────────────────────

def settings() -> dict:
    out = dict(DEFAULTS)
    try:
        blk = json.loads(CONFIG_FILE.read_text()).get("robinhood_view") or {}
        out.update({k: v for k, v in blk.items() if k in DEFAULTS})
    except Exception:
        pass
    return out


def _path(name: str) -> Path:
    return Path(DATA_DIR) / name


def _ensure_dir() -> None:
    d = Path(DATA_DIR)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


def _write_private(name: str, text: str) -> None:
    """Atomic write, readable by Peter only (real balances live in here)."""
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path(name))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_snapshot() -> dict | None:
    try:
        snap = json.loads(_path("snapshot.json").read_text())
        return snap if snap.get("positions") is not None else None
    except Exception:
        return None


def load_status() -> dict:
    try:
        return json.loads(_path("status.json").read_text())
    except Exception:
        return {}


def age_seconds(snap: dict | None, now: float | None = None) -> float | None:
    if not snap or not snap.get("read_ts"):
        return None
    return max(0.0, (now or time.time()) - float(snap["read_ts"]))


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_iso(iso: str | None) -> dt.datetime | None:
    """Aware datetime from an ISO timestamp, or None. Robinhood sends
    nanosecond fractions (which fromisoformat rejects) and "0001-01-01" for
    "never" — both handled."""
    if not iso:
        return None
    s = str(iso).replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in rest:
                tz = sep + rest.split(sep, 1)[1]
                break
        frac = "".join(ch for ch in rest.split("+")[0].split("-")[0] if ch.isdigit())[:6]
        s = head + (f".{frac}" if frac else "") + tz
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    if d.year < 1900:
        return None
    return d


def _epoch(iso: str | None) -> float:
    d = _parse_iso(iso)
    return d.timestamp() if d else 0.0


def _et_date(iso: str | None) -> str:
    """ET calendar date of an ISO timestamp ('' if unparseable)."""
    d = _parse_iso(iso)
    return d.astimezone(ET).date().isoformat() if d else ""


# ── the headless read ────────────────────────────────────────────────────────

def claude_binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for cand in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
                 Path("/usr/local/bin/claude")):
        if cand.exists():
            return str(cand)
    return None


def build_command(binary: str, prompt: str, model: str) -> list[str]:
    return [
        binary, "-p", prompt,
        "--model", model,
        "--setting-sources", "project",      # no user hooks: no voice, no memory saves
        "--no-session-persistence",          # no chat transcript per read
        # The only built-in tool is the tool loader: the Robinhood connector is
        # often still connecting when the run starts, and the loader is how the
        # model waits for it. No shell, no file access.
        "--tools", "ToolSearch",
        "--permission-mode", "dontAsk",      # anything not pre-approved is refused
        "--allowedTools", ",".join(("ToolSearch",) + READ_TOOLS),
        "--disallowedTools", ",".join(BLOCKED_TOOLS),
        "--output-format", "stream-json", "--verbose",
    ]


def _child_env() -> dict:
    """The dashboard loads .env files into its own environment. A stray
    Anthropic key or base URL there would push the read onto paid API billing
    or a different server — Peter uses his subscription login only."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_MODEL"):
        env.pop(k, None)
    return env


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


def parse_stream(lines) -> dict:
    """Robinhood's own answers out of a stream-json transcript, keyed by tool.
    Portfolio/positions keep the account number they were asked about, so a
    read that wandered onto another account can be told apart."""
    calls: dict[str, tuple[str, dict]] = {}
    out = {"accounts": None, "portfolio": [], "positions": [], "quotes": [],
           "historicals": [], "errors": [], "final": None}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        typ = ev.get("type")
        if typ == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    calls[c.get("id", "")] = (c.get("name", ""), c.get("input") or {})
        elif typ == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                    continue
                name, inp = calls.get(c.get("tool_use_id", ""), ("", {}))
                if not name.startswith(PFX):
                    continue
                text = _result_text(c.get("content"))
                if c.get("is_error"):
                    out["errors"].append(f"{name[len(PFX):]}: {text[:160]}")
                    continue
                try:
                    data = json.loads(text).get("data") or {}
                except (ValueError, AttributeError):
                    out["errors"].append(f"{name[len(PFX):]}: unreadable answer")
                    continue
                short = name[len(PFX):]
                acct = str(inp.get("account_number", ""))
                if short == "get_accounts":
                    out["accounts"] = data.get("accounts") or []
                elif short == "get_portfolio":
                    out["portfolio"].append((acct, data))
                elif short == "get_equity_positions":
                    out["positions"].append((acct, data))
                elif short == "get_equity_quotes":
                    out["quotes"].extend(data.get("results") or [])
                elif short == "get_equity_historicals":
                    out["historicals"].extend(data.get("results") or [])
        elif typ == "result":
            out["final"] = ev
    return out


def _closes(q: dict, bars: list[dict]) -> tuple[float, str, float, str]:
    """(latest official close, its date, the close before it, its date).

    Robinhood's quote "previous close" rolls over the moment a session ends,
    and its daily history marks the newest day as a placeholder until it
    settles. So: the latest close comes from the quote's official close, and
    the one before it from the newest NON-placeholder daily bar dated earlier.
    """
    quote = q.get("quote") or {}
    close = q.get("close") or {}
    latest = _f(close.get("price") or quote.get("previous_close"))
    latest_date = str(close.get("date") or quote.get("previous_close_date") or "")
    prior, prior_date = 0.0, ""
    real = [b for b in bars if not b.get("interpolated")
            and str(b.get("begins_at", ""))[:10] < latest_date]
    if real:
        prior = _f(real[-1].get("close_price"))
        prior_date = str(real[-1].get("begins_at", ""))[:10]
    if prior <= 0:                             # no history: no day change rather than a wrong one
        prior, prior_date = latest, latest_date
    return latest, latest_date, prior, prior_date


def build_snapshot(raw: dict, now: float, market_open: bool | None = None) -> dict:
    accts = raw.get("accounts")
    if not accts:
        hint = "; ".join(raw.get("errors") or []) or "no answer"
        raise ReaderError(f"Robinhood did not list the accounts ({hint}) — the "
                          "connection on claude.ai may need approving again")
    main = [a for a in accts if a.get("is_default")]
    if len(main) != 1:
        raise ReaderError("could not tell which Robinhood account is the main one")
    main = main[0]
    num = str(main.get("account_number", ""))
    ports = [d for a, d in raw.get("portfolio") or [] if a == num]
    pages = [d for a, d in raw.get("positions") or [] if a == num]
    if not ports:
        raise ReaderError("Robinhood did not return the main account's value")
    if not pages:
        raise ReaderError("Robinhood did not return the main account's holdings")
    port = ports[-1]

    held = []
    for page in pages:
        for p in page.get("positions") or []:
            if _f(p.get("quantity")) > 0:
                held.append(p)
    quotes = {}
    for r in raw.get("quotes") or []:
        sym = ((r.get("quote") or {}).get("symbol") or "").upper()
        if sym:
            quotes[sym] = r
    hist = {(r.get("symbol") or "").upper(): r.get("bars") or []
            for r in raw.get("historicals") or []}

    rows = []
    for p in held:
        sym = str(p.get("symbol", "")).upper()
        q = quotes.get(sym)
        if not q:
            raise ReaderError(f"no Robinhood price came back for {sym}")
        quote = q.get("quote") or {}
        latest, latest_date, prior, prior_date = _closes(q, hist.get(sym, []))
        # Whichever trade is newer — the regular session's or the after-hours
        # one. Robinhood's own account value counts after-hours prices, so
        # the dashboard does too (checked 2026-10-13: matches to ~$1).
        px, px_time = _f(quote.get("last_trade_price")), quote.get("venue_last_trade_time") or ""
        ext_px, ext_time = _f(quote.get("last_non_reg_trade_price")), quote.get("venue_last_non_reg_trade_time") or ""
        extended = ext_px > 0 and _epoch(ext_time) > _epoch(px_time)
        if extended:
            px, px_time = ext_px, ext_time
        avg = p.get("average_buy_price")
        rows.append({
            "symbol": sym,
            "qty": _f(p.get("quantity")),
            "intraday_qty": _f(p.get("intraday_quantity")),
            "avg_cost": _f(avg) if avg not in (None, "") else None,
            "last_price": px,
            "last_trade_time": px_time,
            "last_trade_date": _et_date(px_time),
            "extended_hours": extended,
            "latest_close": latest, "latest_close_date": latest_date,
            "prior_close": prior, "prior_close_date": prior_date,
            "state": quote.get("state") or "",
        })

    total = _f(port.get("total_value"))
    equity_value = _f(port.get("equity_value"))
    cash = _f(port.get("cash"))
    bp = port.get("buying_power")
    bp = _f(bp.get("buying_power")) if isinstance(bp, dict) else _f(bp)
    return {
        "version": 1,
        "read_ts": now,
        "read_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "market_open": market_open,
        "account_last4": num[-4:],
        "account_type": main.get("brokerage_account_type") or "",
        "total_value": total,
        "cash": cash,
        "buying_power": bp,
        # options/crypto/etc. — whatever is in the total that is not stocks or cash
        "other_value": round(total - equity_value - cash, 6),
        "positions": rows,
    }


def run_reader(market_open: bool | None = None, runner=subprocess.run,
               now_fn=time.time) -> tuple[bool | None, str]:
    """One read. On success the snapshot + history are updated; on failure the
    last good snapshot is kept and the error is recorded in status.json.
    Returns (None, msg) when another read already holds the lock."""
    import fcntl

    st = settings()
    _ensure_dir()
    lock = open(_path(".reader.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None, "a Robinhood read is already running"
    status = load_status()
    status["last_attempt_ts"] = now_fn()
    try:
        binary = claude_binary()
        if not binary:
            raise ReaderError("Claude Code was not found on this Mac")
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=12)
                 ).strftime("%Y-%m-%dT00:00:00Z")
        cmd = build_command(binary, PROMPT.format(start=start), st["model"])
        try:
            proc = runner(cmd, cwd=str(DATA_DIR), capture_output=True, text=True,
                          timeout=int(st["timeout_seconds"]), env=_child_env())
        except subprocess.TimeoutExpired:
            raise ReaderError("the Robinhood read timed out")
        raw = parse_stream((proc.stdout or "").splitlines())
        if raw["accounts"] is None and (proc.returncode or not proc.stdout):
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise ReaderError(f"Claude Code could not run the read: {tail[0][:160]}")
        snap = build_snapshot(raw, now_fn(), market_open)
    except ReaderError as e:
        status["last_error"] = str(e)
        status["consecutive_failures"] = int(status.get("consecutive_failures") or 0) + 1
        _write_private("status.json", json.dumps(status, indent=2))
        lock.close()
        return False, str(e)
    except Exception as e:                       # never crash the dashboard
        status["last_error"] = f"unexpected: {e}"
        status["consecutive_failures"] = int(status.get("consecutive_failures") or 0) + 1
        _write_private("status.json", json.dumps(status, indent=2))
        lock.close()
        return False, status["last_error"]

    _write_private("snapshot.json", json.dumps(snap, indent=2))
    _ensure_dir()
    hp = _path("history.jsonl")
    with open(hp, "a") as f:
        f.write(json.dumps({"ts": snap["read_ts"], "total": snap["total_value"]}) + "\n")
    try:
        os.chmod(hp, 0o600)
    except OSError:
        pass
    status.update({"last_ok_ts": snap["read_ts"], "last_error": "",
                   "consecutive_failures": 0})
    _write_private("status.json", json.dumps(status, indent=2))
    lock.close()
    return True, f"{len(snap['positions'])} holdings, value ${snap['total_value']:,.2f}"


def read_is_due(snap: dict | None, status: dict, market_open: bool | None,
                now: float | None = None, st: dict | None = None) -> bool:
    """Every few minutes while the market is open; one read soon after the
    close (the last snapshot was taken while it was open); otherwise every
    few hours. A failed attempt waits one full interval before retrying."""
    st = st or settings()
    if not st.get("enabled", True):
        return False
    now = now or time.time()
    every = float(st["read_every_minutes"]) * 60
    last_try = _f(status.get("last_attempt_ts"))
    if last_try and now - last_try < every:
        return False
    age = age_seconds(snap, now)
    if age is None:
        return True
    if market_open:
        return age >= every
    if snap.get("market_open"):
        return True
    return age >= float(st["closed_every_hours"]) * 3600


# ── turning a snapshot into dashboard rows ──────────────────────────────────

def fetch_live_prices(symbols: list[str], headers: dict, data_base: str) -> dict:
    """{symbol: (price, iso_time)} from Alpaca's latest trades — used between
    Robinhood reads so the dashboard moves every 8 seconds like the others.
    Symbols Alpaca does not carry are simply absent."""
    if not symbols:
        return {}
    import requests
    try:
        r = requests.get(f"{data_base}/stocks/trades/latest", headers=headers,
                         params={"symbols": ",".join(symbols), "feed": "iex"},
                         timeout=8)
        trades = (r.json() or {}).get("trades") or {}
    except Exception:
        return {}
    out = {}
    for sym, tr in trades.items():
        px = _f((tr or {}).get("p"))
        if px > 0:
            out[sym.upper()] = (px, (tr or {}).get("t") or "")
    return out


def view(snap: dict, live: dict | None = None) -> tuple[dict, list[dict], dict]:
    """(account dict, positions list, info) shaped like the Alpaca data the
    dashboard already renders. Prices: a live Alpaca trade when it is newer
    than Robinhood's last trade, else Robinhood's own price.

    Day change: if the price being shown is from a session AFTER the latest
    official close, it is today's move from that close; otherwise it is the
    latest session's move from the close before it (evenings, weekends)."""
    live = live or {}
    rows, stocks, day_pl, base_total, upl, cost_total = [], 0.0, 0.0, 0.0, 0.0, 0.0
    src_live = 0
    for p in snap.get("positions") or []:
        sym = p["symbol"]
        qty, intr = _f(p.get("qty")), _f(p.get("intraday_qty"))
        price, pdate = _f(p.get("last_price")), p.get("last_trade_date") or ""
        lv = live.get(sym)
        if lv and _epoch(lv[1]) > _epoch(p.get("last_trade_time")):
            price, pdate = lv[0], _et_date(lv[1]) or pdate
            src_live += 1
        if pdate and p.get("latest_close_date") and pdate > p["latest_close_date"]:
            ref = _f(p.get("latest_close"))
        else:
            ref = _f(p.get("prior_close"))
        avg = p.get("avg_cost")
        avg_f = _f(avg) if avg is not None else ref
        held_before = max(0.0, qty - intr)
        base = held_before * ref + intr * avg_f
        dpl = held_before * (price - ref) + intr * (price - avg_f) if ref > 0 else 0.0
        mv = qty * price
        cost = qty * avg_f if avg is not None else 0.0
        pl = (mv - cost) if avg is not None else 0.0
        rows.append({
            "symbol": sym, "qty": qty, "avg_entry_price": avg_f if avg is not None else 0.0,
            "current_price": price, "market_value": mv, "cost_basis": cost,
            "unrealized_pl": pl,
            "unrealized_plpc": (pl / cost) if cost else 0.0,
            "unrealized_intraday_pl": dpl,
            "unrealized_intraday_plpc": (dpl / base) if base else 0.0,
        })
        stocks += mv
        day_pl += dpl
        base_total += base
        upl += pl
        cost_total += cost
    cash = _f(snap.get("cash"))
    total = cash + _f(snap.get("other_value")) + stocks
    acct = {
        "equity": total, "cash": cash, "last_equity": total - day_pl,
        "long_market_value": stocks, "buying_power": _f(snap.get("buying_power")),
    }
    info = {
        "day_pl": day_pl,
        "day_pct": (day_pl / base_total * 100) if base_total else 0.0,
        "upl": upl,
        "upl_pct": (upl / cost_total * 100) if cost_total else 0.0,
        "last4": snap.get("account_last4", ""),
        "live_prices": src_live,
    }
    return acct, rows, info


# ── account-value chart from the dashboard's own reads ──────────────────────

_RANGE_DAYS = {"1D": 1, "1W": 7, "1M": 31, "3M": 92, "6M": 183, "1Y": 366}


def load_history() -> list[tuple[float, float]]:
    out = []
    try:
        with open(_path("history.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out.append((float(r["ts"]), float(r["total"])))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return []
    out.sort()
    return out


def history_bars(range_key: str, points: list[tuple[float, float]] | None = None,
                 now: float | None = None) -> list[dict]:
    """Candles of account value built from successive reads: one candle per
    read on 1D (the latest day that has reads), per hour on 1W, per day beyond.
    Robinhood offers no account-value history, so this fills in over time."""
    pts = load_history() if points is None else sorted(points)
    if not pts:
        return []
    now = now or time.time()
    if range_key == "1D":
        last_day = dt.datetime.fromtimestamp(pts[-1][0], ET).date()
        pts = [p for p in pts if dt.datetime.fromtimestamp(p[0], ET).date() == last_day]
        bucket = None
    else:
        cutoff = now - _RANGE_DAYS.get(range_key, 31) * 86400
        pts = [p for p in pts if p[0] >= cutoff]
        bucket = "%Y-%m-%d %H" if range_key == "1W" else "%Y-%m-%d"
    bars, prev = [], None
    if bucket is None:
        for ts, v in pts:
            o = v if prev is None else prev
            bars.append({"t": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(),
                         "o": o, "h": max(o, v), "l": min(o, v), "c": v})
            prev = v
        return bars
    groups: dict[str, list[tuple[float, float]]] = {}
    for ts, v in pts:
        groups.setdefault(dt.datetime.fromtimestamp(ts, ET).strftime(bucket), []).append((ts, v))
    for key in sorted(groups):
        g = groups[key]
        vals = [v for _, v in g]
        o = vals[0] if prev is None else prev
        bars.append({"t": dt.datetime.fromtimestamp(g[-1][0], dt.timezone.utc).isoformat(),
                     "o": o, "h": max(vals + [o]), "l": min(vals + [o]), "c": vals[-1]})
        prev = vals[-1]
    return bars


# ── command line ─────────────────────────────────────────────────────────────

def _print_snapshot(snap: dict | None) -> None:
    if not snap:
        print("no Robinhood snapshot yet")
        return
    acct, rows, info = view(snap)
    age = age_seconds(snap) or 0
    print(f"Robinhood main ••{info['last4']}  read {age/60:.0f} min ago")
    print(f"  value ${acct['equity']:,.2f}  stocks ${acct['long_market_value']:,.2f}  "
          f"cash ${acct['cash']:,.2f}  day {info['day_pl']:+,.2f} ({info['day_pct']:+.2f}%)  "
          f"unrealized {info['upl']:+,.2f} ({info['upl_pct']:+.2f}%)")
    for r in rows:
        print(f"  {r['symbol']:6} {r['qty']:>10.4f}  ${r['current_price']:>10,.2f}  "
              f"value ${r['market_value']:>9,.2f}  day {r['unrealized_intraday_pl']:+8,.2f}  "
              f"total {r['unrealized_pl']:+8,.2f}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "read":
        ok, msg = run_reader()
        print(("OK: " if ok else "FAILED: ") + msg)
        _print_snapshot(load_snapshot())
        return 0 if ok else 1
    if cmd == "show":
        _print_snapshot(load_snapshot())
        st = load_status()
        if st.get("last_error"):
            print(f"last error: {st['last_error']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
