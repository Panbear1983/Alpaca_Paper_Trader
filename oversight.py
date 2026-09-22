#!/usr/bin/env python3
"""
oversight.py — daily review of what the High Risk wallet did.

Two stages, deliberately doing different jobs:

  check     at the close (04:00 Taipei). Pure arithmetic, no model. Silent
            unless a threshold breaks, so a normal day produces no 4am push.

  evaluate  with the morning report (09:00 Taipei). Invokes the Claude CLI on a
            compact facts pack and appends its judgement to the report.

Chain of authority: this evaluates and proposes, Peter approves, Claude writes
the config (since 2026-08-30 — WB had marked four proposals "done" without
writing them), WB trades inside it. Proposals live in diary/proposals.jsonl.
The guardrails are enforced in code with hard ceilings, so an approved tweak
can move tuning knobs but cannot raise a cap or disable a stop.

  python3 oversight.py check
  python3 oversight.py evaluate [--session YYYY-MM-DD] [--no-push]
  python3 oversight.py list | approve <id> | reject <id>
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

ET = ZoneInfo("America/New_York")
DIARY = _HERE / "diary"
PROPOSALS = DIARY / "proposals.jsonl"
GUARDRAIL_LOG = DIARY / "guardrail_log.jsonl"
BRIEF = DIARY / "oversight.md"
LESSONS = DIARY / "lessons.md"
NOTE = DIARY / "note_to_wb.md"           # today's nudge — rewritten daily, expires
DIRECTION = DIARY / "direction.md"       # what Peter last signed off
INSTRUCTION_LOG = DIARY / "instruction_log.jsonl"
MAX_LESSONS = 10

# WB's instruction surface. SOUL.md and MEMORY.md live outside the repo and are
# tracked by nothing, so without this log there is no record of what Claude told
# WB or when — and the weekly sign-off would be ceremonial.
WB_HOME = Path.home() / ".hermes/profiles/wanna_buffet"
WATCHED = {
    "soul":    WB_HOME / "SOUL.md",
    "memory":  WB_HOME / "memories/MEMORY.md",
    "lessons": LESSONS,
    "note":    NOTE,
}
CHECK_LOG = DIARY / "check_log.jsonl"

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
CLAUDE_TIMEOUT = int(os.environ.get("OVERSIGHT_CLAUDE_TIMEOUT", "300"))

# Thresholds for the silent close-of-day check.
TH = {
    "day_loss_pct":      -2.0,   # session P&L at or below this
    "min_profit_factor":  0.8,   # across at least min_trades attributed trades
    "min_trades":         20,
    "churn_x_equity":     2.0,   # gross traded in one session
    "quiet_sessions":     3,     # an enabled engine placing nothing this long
}


# ── instruction audit trail ──────────────────────────────────────────────────

def _digest(path: Path) -> str:
    import hashlib
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    except Exception:
        return ""


def log_instruction(which: str, summary: str, why: str = "", diff: str = "") -> None:
    """Record a change to something WB reads.

    Peter is out of the daily loop by his own choice, so the weekly report is
    his only view of what I told WB. That report is worth signing only if it is
    backed by a record he could check independently — this is that record.
    """
    try:
        DIARY.mkdir(parents=True, exist_ok=True)
        row = {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"),
               "file": which, "summary": summary, "why": why,
               "digest": _digest(WATCHED.get(which, Path("/nonexistent"))),
               "diff": diff[:4000]}
        with open(INSTRUCTION_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"[oversight] could not log instruction: {e}")


def write_note(text: str, why: str = "") -> str:
    """Today's private nudge to WB. Replaces yesterday's — it is meant to expire.

    Daily guidance does NOT go in SOUL.md: stable rules should stay stable, and
    rewriting them nightly is how an instruction set drifts into contradicting
    itself. WB's MEMORY.md had already collected duplicates and two junk 'Test'
    entries before anyone looked.
    """
    import difflib
    old = NOTE.read_text() if NOTE.exists() else ""
    sess = dt.datetime.now(ET).date().isoformat()
    body = (f"# Note to WB — {sess}\n\n"
            f"Written by Claude. This replaces yesterday's note; it is guidance for today, "
            f"not a standing rule. Standing rules are in SOUL.md and lessons.md.\n\n{text.strip()}\n")
    DIARY.mkdir(parents=True, exist_ok=True)
    NOTE.write_text(body)
    diff = "\n".join(difflib.unified_diff(old.splitlines(), body.splitlines(),
                                          "previous", "current", lineterm="", n=1))
    log_instruction("note", text.strip().split("\n")[0][:120], why, diff)
    return f"note written for {sess}"


def current_direction() -> str:
    return DIRECTION.read_text() if DIRECTION.exists() else "(no direction signed off yet)"


def record_signoff(note: str) -> str:
    sess = dt.datetime.now(ET).date().isoformat()
    prev = DIRECTION.read_text() if DIRECTION.exists() else ""
    DIRECTION.write_text(f"# Direction — signed off {sess}\n\n{note.strip()}\n\n"
                         f"---\n_Previous:_\n\n{prev}"[:8000])
    log_instruction("direction", f"Peter signed off: {note.strip()[:100]}", "weekly sign-off")
    return f"direction recorded {sess}"


# ── data ─────────────────────────────────────────────────────────────────────

def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def per_engine() -> dict:
    """Trades, win rate, profit factor, median hold and net, per engine.

    The whole point of stamping client_order_id — before that every engine's
    trades were pooled under one guessed label and could not be separated.
    """
    try:
        import performance_tracker as pt
        trades = pt.load_log().get("trades") or []
    except Exception:
        return {}
    by = collections.defaultdict(list)
    for t in trades:
        by[t.get("strategy") or "unattributed"].append(t)
    out = {}
    for src, ts in by.items():
        wins = [t for t in ts if t.get("pnl_usd", 0) > 0]
        losses = [t for t in ts if t.get("pnl_usd", 0) <= 0]
        gp = sum(t.get("pnl_usd", 0) for t in wins)
        gl = abs(sum(t.get("pnl_usd", 0) for t in losses))
        # hold_hours comes from real fill timestamps; hold_days is integer days
        # and reads 0 for every intraday round trip, which is most of them.
        holds = [t["hold_hours"] for t in ts if t.get("hold_hours") is not None]
        out[src] = {
            "n": len(ts),
            "win_pct": len(wins) / len(ts) * 100 if ts else 0.0,
            "net": gp - gl,
            "pf": (gp / gl) if gl else float("inf"),
            "median_hold_h": statistics.median(holds) if holds else None,
        }
    return out


def session_facts(session: str | None = None) -> dict:
    """The day itself, reusing what the daily report already assembles."""
    import wb_daily_report as wb
    d, sess = wb.gather_report_data(session)
    return d, sess


def guardrail_hits(session: str) -> list[dict]:
    return [r for r in _jsonl(GUARDRAIL_LOG) if str(r.get("ts", "")).startswith(session)]


def history(limit: int = 10) -> list[dict]:
    return _jsonl(DIARY / "history.jsonl")[-limit:]


# ── stage 1: the silent close-of-day check ───────────────────────────────────

def run_check(push: bool = True) -> list[str]:
    """Threshold arithmetic. Returns the list of breaches (empty = all well)."""
    breaches: list[str] = []
    d, sess = session_facts()

    pl, pct = d.get("day_pl"), d.get("day_pct")
    if pct is not None and pct <= TH["day_loss_pct"]:
        breaches.append(f"Day {pl:+,.0f} ({pct:+.2f}%) — at or past the {TH['day_loss_pct']}% line.")

    eng = per_engine()
    for src, s in sorted(eng.items()):
        if src == "unattributed":
            continue
        if s["n"] >= TH["min_trades"] and s["pf"] < TH["min_profit_factor"]:
            breaches.append(f"{src}: profit factor {s['pf']:.2f} over {s['n']} trades "
                            f"(under {TH['min_profit_factor']}).")

    trades = d.get("trades") or []
    eq = d.get("equity") or 0
    gross = sum(t.get("usd", 0) for t in trades)
    if eq and gross > eq * TH["churn_x_equity"]:
        breaches.append(f"Churn: ${gross:,.0f} traded = {gross/eq:.1f}x equity in one session.")

    hits = guardrail_hits(sess)
    if hits:
        caps = collections.Counter(h.get("cap") for h in hits)
        breaches.append(f"Guardrails blocked {len(hits)} order(s): "
                        + ", ".join(f"{k} x{v}" for k, v in caps.items()))

    try:
        _, ab = anchor_facts(sess)
        breaches.extend(ab)
    except Exception as e:
        breaches.append(f"anchor check could not run: {e}")

    hist = history(2)
    try:
        import market_context as mc
        now_state = mc.regime_state().get("state")
        prev_state = (hist[-2].get("regime_state") if len(hist) > 1 else None)
        if prev_state and now_state and prev_state != now_state:
            breaches.append(f"Regime flipped {prev_state} -> {now_state}.")
    except Exception:
        pass

    stamp = {"ts": dt.datetime.now(ET).isoformat(timespec="seconds"),
             "session": sess, "breaches": breaches}
    try:
        DIARY.mkdir(parents=True, exist_ok=True)
        with open(CHECK_LOG, "a") as f:
            f.write(json.dumps(stamp) + "\n")
    except Exception:
        pass

    if breaches and push:
        try:
            import telegram_notifier as tn
            tn.send("*High Risk — close check* " + sess + "\n\n"
                    + "\n".join(f"• {b}" for b in breaches), channel="home")
        except Exception as e:
            print(f"[oversight] telegram failed: {e}")
    return breaches


# ── stage 2: the evaluation ──────────────────────────────────────────────────

PROMPT = """You oversee a paper-trading wallet on Peter's behalf. Below are today's
facts. Judge them.

Rules:
- Use ONLY the facts given. Never invent a number, ticker or event.
- Say plainly when the data is too thin to conclude anything. Most single days are.
- You are not writing a report Peter already has — he has the numbers. Say what
  they MEAN and what you would change.

Write:
1. READ: two or three sentences. What actually happened and whether it matters.
2. WATCH: one line — what you are tracking into tomorrow.
3. PROPOSALS: zero or more, each on its own line as
   PROPOSE | <config.key> <old> -> <new> | <why, citing a figure above>
   Only propose a change you can express as ONE config key. Propose nothing at
   all if the day does not justify it — silence is a valid answer and a better
   one than noise.
4. LESSONS: zero or more, each on its own line as
   LESSON | <one sentence rule> | <the figure that proves it>
   A lesson is something the trading agent should DO DIFFERENTLY that no config
   key can express — when to sit out, how to weigh conflicting signals. It must
   be earned from this account's own record, never general trading advice, and
   never a restatement of an existing lesson below. One day of data almost never
   earns a lesson. Propose none unless the evidence is strong.

--- LESSONS ALREADY LEARNED (do not repeat these) ---
{lessons}

--- STANDING BRIEF (what you concluded before) ---
{brief}
--- TODAY ---
{facts}
--- END ---
"""


# ── anchor plan: what WB did today, against what the plan allows ─────────────

def anchor_facts(sess: str) -> tuple[list[str], list[str]]:
    """(fact lines, breach lines) for the session. Reads Alpaca fills directly, so an order
    placed around the fence is seen here even though it never touched our code."""
    import anchor_journal as aj
    import entry_gate as eg
    import performance_tracker as pt
    lines, breaches = [], []
    try:
        cfg = eg.load_cfg()
    except Exception as e:
        return [f"anchor: config unreadable ({e})"], []
    if not cfg.get("enabled"):
        return ["anchor plan: fence OFF"], []
    universe = {str(u).upper() for u in cfg.get("universe") or []}
    lo, hi = cfg.get("entry_window_et") or ["09:30", "11:30"]
    noon = dt.datetime.combine(dt.date.fromisoformat(sess), dt.time(12, 0), tzinfo=ET)
    try:
        orders = eg.fetch_todays_orders(noon)
    except Exception as e:
        return [f"anchor: could not read the day's orders ({e})"], []
    buys = [o for o in orders if o.get("side") == "buy" and o.get("status") == "filled"]
    sells = [o for o in orders if o.get("side") == "sell" and o.get("status") == "filled"]

    def _et(o):
        try:
            return dt.datetime.fromisoformat(str(o.get("filled_at")).replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            return None
    per_name = collections.Counter(o.get("symbol") for o in buys)
    outside, nonanchor = [], []
    for o in buys:
        t = _et(o)
        if o.get("symbol") not in universe:
            nonanchor.append(o.get("symbol"))
        if t and not (eg._parse_hhmm(lo) <= t.time() <= eg._parse_hhmm(hi)):
            outside.append(f"{o.get('symbol')} {t:%H:%M}")
    bypass = [o for o in orders if o.get("status") == "filled" and pt.source_of(o) == "unattributed"]
    trims = [h for h in guardrail_hits(sess) if h.get("cap") == "trim"]
    blocks = collections.Counter(h.get("cap") for h in guardrail_hits(sess) if h.get("cap") != "trim")
    closed = [r for r in aj.read() if r.get("status") == "closed" and str(r.get("closed_at", ""))[:10] == sess]
    opened = [r for r in aj.read() if str(r.get("ts", ""))[:10] == sess]

    lines.append("ANCHOR PLAN (five names, 09:30-11:30 entries):")
    lines.append(f"  buys filled: {len(buys)} ({', '.join(f'{k} x{v}' for k, v in per_name.items()) or 'none'}); "
                 f"sells filled: {len(sells)}")
    lines.append(f"  entries via anchor_trade.py: {len(opened)}; closed anchor trades today: {len(closed)}")
    for r in closed:
        lines.append(f"    {r.get('symbol')} {r.get('reason')} pnl {float(r.get('pnl') or 0):+,.0f} "
                     f"({r.get('r_multiple')}R) — {str(r.get('note',''))[:60]}")
    if trims:
        lines.append(f"  trim-to-cap sells: {len(trims)} — " + "; ".join(str(h.get('reason'))[:70] for h in trims[:6]))
    if blocks:
        lines.append("  fence refusals: " + ", ".join(f"{k} x{v}" for k, v in blocks.items()))
    if nonanchor:
        lines.append(f"  NON-ANCHOR BUYS: {', '.join(nonanchor)}")
        breaches.append(f"{len(nonanchor)} buy(s) in non-anchor names filled: {', '.join(sorted(set(nonanchor)))}.")
    if outside:
        lines.append(f"  BUYS OUTSIDE THE WINDOW: {', '.join(outside)}")
        breaches.append(f"{len(outside)} buy(s) filled outside {lo}-{hi} ET: {', '.join(outside)}.")
    if bypass:
        syms = sorted({o.get('symbol') for o in bypass})
        lines.append(f"  ORDERS AROUND THE FENCE (no engine stamp): {len(bypass)} — {', '.join(syms)}")
        breaches.append(f"{len(bypass)} order(s) bypassed the fence (placed directly against the API): {', '.join(syms)}.")
    if trims:
        breaches.append(f"Trim-to-cap sold {len(trims)} slice(s) to bring the book under the caps (expected on 08-31).")
    return lines, breaches


def build_facts(d: dict, sess: str) -> str:
    L = [f"session: {sess}"]
    if d.get("day_pl") is not None:
        L.append(f"day P&L: {d['day_pl']:+,.2f} ({d['day_pct']:+.2f}%), equity {d['equity']:,.2f}")
    rl = d.get("realized") or {}
    if rl:
        L.append("realized by symbol: " + ", ".join(f"{k} {v:+.0f}" for k, v in sorted(rl.items(), key=lambda kv: kv[1])))
    L.append(f"orders: {len(d.get('trades') or [])}")
    held = d.get("held") or []
    if held:
        L.append(f"holdings ({len(held)}): " + ", ".join(f"{h['sym']} ${h['mv']:,.0f}" for h in held[:15]))

    eng = per_engine()
    if eng:
        L.append("\nPER-ENGINE (all closed trades to date):")
        for src, s in sorted(eng.items(), key=lambda kv: -kv[1]["n"]):
            pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            mh = f"{s['median_hold_h']:.1f}h" if s["median_hold_h"] is not None else "-"
            L.append(f"  {src:14s} n={s['n']:<4d} win={s['win_pct']:.0f}%  net={s['net']:+,.0f}  PF={pf}  median hold={mh}")

    hits = guardrail_hits(sess)
    L.append(f"\nguardrails blocked today: {len(hits)}")
    for h in hits[:8]:
        L.append(f"  {h.get('source','?')} {h.get('symbol')} — {h.get('cap')} cap: {str(h.get('reason'))[:90]}")

    try:
        al, _ = anchor_facts(sess)
        L.append("\n" + "\n".join(al))
    except Exception as e:
        L.append(f"\nanchor facts unavailable: {e}")

    try:
        import market_context as mc
        ctx = mc.build(held, sess)
        L.append("\n" + mc.as_facts(ctx))
    except Exception:
        pass

    hist = history(5)
    if hist:
        L.append("\nRECENT SESSIONS:")
        for h in hist:
            L.append(f"  {h.get('session')} P&L {h.get('day_pl', 0):+,.0f} orders {h.get('n_orders', 0)}")
    return "\n".join(L)


def run_evaluate(session: str | None = None, push: bool = True) -> dict:
    d, sess = session_facts(session)
    facts = build_facts(d, sess)
    brief = BRIEF.read_text() if BRIEF.exists() else "(no prior brief — this is the first run)"
    lessons = LESSONS.read_text() if LESSONS.exists() else "(none yet)"

    if not (os.path.isfile(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)):
        return {"ok": False, "error": f"claude CLI not found at {CLAUDE_BIN}"}
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", PROMPT.format(brief=brief[-3000:], facts=facts, lessons=lessons[-4000:])],
                           capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=str(_HERE))
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"claude timed out after {CLAUDE_TIMEOUT}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not out:
        return {"ok": False, "error": (r.stderr or "empty response")[:200]}

    proposals = []
    body = []
    for line in out.splitlines():
        head = line.strip().upper()
        if head.startswith("PROPOSE |") or head.startswith("LESSON |"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                proposals.append({
                    "id": f"{sess}-{len(_jsonl(PROPOSALS)) + len(proposals) + 1}",
                    "session": sess, "status": "pending",
                    "type": "lesson" if head.startswith("LESSON") else "config",
                    "what": parts[1], "why": parts[2],
                    "created": dt.datetime.now(ET).isoformat(timespec="seconds"),
                })
        else:
            body.append(line)
    text = "\n".join(body).strip()

    DIARY.mkdir(parents=True, exist_ok=True)
    if proposals:
        with open(PROPOSALS, "a") as f:
            for p in proposals:
                f.write(json.dumps(p) + "\n")
    BRIEF.write_text(f"# Oversight brief — last updated {sess}\n\n{text}\n")

    return {"ok": True, "session": sess, "text": text, "proposals": proposals}


WEEKLY_PROMPT = """You have been supervising a trading agent ("WB") for a week on Peter's
behalf. He is not in the daily loop — this report is his whole view of it, and he
signs off the direction for next week based on it.

Write it as you would speak to him. Plain English. No tables, no ratios he has to
decode, no jargon without a plain gloss. Short is good; he reads this on a phone.

Cover four things, in this order, with a blank line between each:

1. WHAT I TOLD WB THIS WEEK, AND WHY — in sentences, from the instruction log below.
   If I told it nothing, say so plainly.
2. WHETHER IT LISTENED — the part he actually wants. Compare what changed in the
   trading against what was asked. Where you cannot tell, say you cannot tell.
3. WHAT THE WEEK SAYS — the trading result, honestly. Most weeks the sample is too
   small to conclude anything; say that rather than manufacturing a narrative.
4. WHERE I WANT TO TAKE IT, AND WHAT NEEDS YOUR DECISION — one clear ask, or none.

Rules:
- Use ONLY the facts below. Never invent a number or an event.
- Do not repeat last week's direction back at him as if it were new.
- If the honest answer is "nothing meaningful happened, keep going", write that.
  A short honest report beats a long one that pads.

--- DIRECTION HE LAST SIGNED OFF ---
{direction}
--- WHAT I TOLD WB THIS WEEK ---
{instructions}
--- THE WEEK'S TRADING ---
{facts}
--- END ---
"""


def run_weekly(push: bool = False) -> dict:
    """The Saturday report. Rides Friday 21:00 ET, after Friday's close."""
    d, sess = session_facts()
    facts = build_facts(d, sess)

    since = (dt.datetime.now(ET) - dt.timedelta(days=8)).isoformat()
    rows = [r for r in _jsonl(INSTRUCTION_LOG) if r.get("ts", "") >= since]
    if rows:
        instructions = "\n".join(
            f"- {r['ts'][:10]} [{r['file']}] {r['summary']}"
            + (f"  (why: {r['why']})" if r.get("why") else "") for r in rows)
    else:
        instructions = "(nothing — I gave WB no new instructions this week)"

    hist = history(6)
    if hist:
        facts += "\n\nTHE WEEK, SESSION BY SESSION:\n" + "\n".join(
            f"  {h.get('session')}  P&L {h.get('day_pl', 0):+,.0f}  orders {h.get('n_orders', 0)}"
            for h in hist)

    if not (os.path.isfile(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)):
        return {"ok": False, "error": f"claude CLI not found at {CLAUDE_BIN}"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", WEEKLY_PROMPT.format(
                direction=current_direction()[:2000],
                instructions=instructions[:4000], facts=facts)],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=str(_HERE))
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"claude timed out after {CLAUDE_TIMEOUT}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not out:
        return {"ok": False, "error": (r.stderr or "empty response")[:200]}

    if push:
        try:
            import telegram_notifier as tn
            for part in __import__("hermes_report")._split_for_telegram(
                    f"*Weekly assessment — week ending {sess}*\n\n{out}"):
                tn.send(part, channel="home")
        except Exception as e:
            print(f"[oversight] telegram failed: {e}")
    return {"ok": True, "session": sess, "text": out, "n_instructions": len(rows)}


# ── proposals ────────────────────────────────────────────────────────────────

def _rewrite(rows: list[dict]) -> None:
    PROPOSALS.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


def cmd_list() -> None:
    rows = _jsonl(PROPOSALS)
    if not rows:
        print("no proposals"); return
    for r in rows:
        print(f"  [{r.get('status','?'):8s}] {r.get('type','config'):6s} {r['id']}  {r.get('what')}")
        print(f"             {r.get('why','')}")


def _append_lesson(row: dict) -> str:
    """Write an approved lesson into lessons.md.

    Config changes are WB's to apply; a lesson is not — it belongs in the file
    WB reads, and letting WB write its own lessons would make the approval gate
    meaningless. Bounded at MAX_LESSONS because its MEMORY.md had already
    drifted into duplicates and junk entries before anyone noticed.
    """
    body = LESSONS.read_text() if LESSONS.exists() else "# What we have learned trading this wallet\n"
    n = body.count("\n## ")
    if n >= MAX_LESSONS:
        return (f"lessons.md already holds {n} (ceiling {MAX_LESSONS}). "
                "Remove a weaker one first — this file earns its weight by staying short.")
    body = body.rstrip("\n") + (
        f"\n\n---\n\n## {n + 1}. {row['what']}\n\n{row['why']}\n\n"
        f"*Approved {dt.datetime.now(ET).date().isoformat()} from proposal {row['id']}.*\n")
    LESSONS.write_text(body)
    return f"written to {LESSONS.name} as lesson {n + 1}"


def cmd_set_status(pid: str, status: str) -> None:
    rows = _jsonl(PROPOSALS)
    hit = None
    for r in rows:
        if r.get("id") == pid:
            r["status"] = status
            r[f"{status}_at"] = dt.datetime.now(ET).isoformat(timespec="seconds")
            hit = r
    if not hit:
        print(f"no proposal with id {pid}"); return
    note = ""
    if status == "approved" and hit.get("type") == "lesson":
        note = " — " + _append_lesson(hit)
        hit["status"] = "done"      # lessons land immediately; WB only applies config
        hit["done_at"] = dt.datetime.now(ET).isoformat(timespec="seconds")
    _rewrite(rows)
    print(f"{pid} -> {hit['status']}{note}")


def main() -> int:
    a = sys.argv[1:] or ["check"]
    cmd = a[0]
    if cmd == "check":
        b = run_check(push="--no-push" not in a)
        print("\n".join(f"BREACH: {x}" for x in b) if b else "all thresholds clear")
        return 0
    if cmd == "evaluate":
        sess = a[a.index("--session") + 1] if "--session" in a else None
        res = run_evaluate(sess, push="--no-push" not in a)
        if not res.get("ok"):
            print(f"evaluate failed: {res.get('error')}"); return 1
        print(res["text"])
        for p in res["proposals"]:
            print(f"\nPROPOSAL {p['id']}: {p['what']}\n   {p['why']}")
        return 0
    if cmd == "weekly":
        res = run_weekly(push="--push" in a)
        if not res.get("ok"):
            print(f"weekly failed: {res.get('error')}"); return 1
        print(res["text"]); return 0
    if cmd == "note":
        text = a[1] if len(a) > 1 else sys.stdin.read()
        why = a[a.index("--why") + 1] if "--why" in a else ""
        print(write_note(text, why)); return 0
    if cmd == "signoff":
        if len(a) < 2:
            print("usage: oversight.py signoff \"<direction>\""); return 1
        print(record_signoff(a[1])); return 0
    if cmd == "direction":
        print(current_direction()); return 0
    if cmd == "log":
        for r in _jsonl(INSTRUCTION_LOG)[-20:]:
            print(f"  {r['ts'][:16]} [{r['file']:9s}] {r['summary']}")
        return 0
    if cmd == "list":
        cmd_list(); return 0
    if cmd in ("approve", "reject") and len(a) > 1:
        cmd_set_status(a[1], "approved" if cmd == "approve" else "rejected"); return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
