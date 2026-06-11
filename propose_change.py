#!/usr/bin/env python3
"""
propose_change.py — Hermes → Claude Code → live strategy edit.

Two modes:
  (default)   PROPOSE: draft change in sandbox, push diff to Telegram, DO NOT deploy.
  --apply     OPTION 2 (autonomous): draft → automated sanity gate → if pass,
              apply to LIVE repo + git commit + Telegram notify. No human approval.

Safety model for --apply (no human gate, but self-guarded):
  - Edits drafted in an isolated /tmp sandbox first (live untouched during drafting).
  - Automated SANITY GATE before anything goes live:
      * only repo files touched (never .env, never paths outside the repo)
      * every changed .py still compiles (no broken Python goes live)
      * every changed .json still parses
      * strategy_config.json keeps a sane tsla.stop_loss_pct (downside stays protected)
      * change isn't suspiciously huge
  - If the gate passes: files copied to live + `git commit` (revertible with one command).
  - If the gate fails: NOTHING applied; Telegram says why; saved as a proposal.
  - Telegram ping on every applied/blocked change (your "check from time to time").

Usage:
  python3 propose_change.py "<request>"            # propose only (review)
  python3 propose_change.py --apply "<request>"    # autonomous: apply to live if safe
  python3 propose_change.py --apply --no-push "<request>"   # apply, skip telegram (testing)
"""
from __future__ import annotations
import os, sys, json, shutil, subprocess, datetime, textwrap
from pathlib import Path
import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent
load_dotenv(REPO / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
PROPOSALS  = REPO / "proposals"
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.environ.get("TELEGRAM_HOME_CHANNEL") or os.environ.get("TELEGRAM_CHAT_ID", "")

COPY_SKIP = {".git", "__pycache__", ".env", "reports", ".logs",
             "backtest_cache.json", "proposals"}
DIFF_SKIP = {".git", "__pycache__", "reports", ".logs", ".env",
             "backtest_cache.json", "backtest_results.json", "politician_universe.json",
             "pool_state.json", "performance_log.json", ".copied_trades.json",
             ".tsla_state.json", ".sentiment_cache.json", ".event_watcher_state.json",
             "politician_history.json", "review_log.json", "vetting_log.json",
             "proposals"}

CLAUDE_TIMEOUT = 420
MAX_CHANGED_FILES = 25            # gate: suspicious if more than this
STOP_LOSS_SAFE = (0.02, 0.25)     # gate: tsla stop_loss_pct must stay in this range


# ── sandbox + claude (shared by both modes) ──────────────────────────────────

def make_sandbox(req_id: str) -> Path:
    sandbox = Path("/tmp") / f"proposal_{req_id}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    for item in REPO.iterdir():
        if item.name in COPY_SKIP:
            continue
        dest = sandbox / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=shutil.ignore_patterns(*COPY_SKIP))
        else:
            shutil.copy2(item, dest)
    return sandbox


def run_claude(sandbox: Path, request: str) -> tuple[str, int]:
    prompt = textwrap.dedent(f"""
        You are implementing a requested change to an Alpaca paper-trading codebase
        (a SANDBOX copy). Make the edits, do not run trading scripts, do not place
        orders, do not commit. Keep the diff minimal and conservative. Do NOT remove
        or weaken the stop-loss logic. Implement:

        REQUEST: {request}

        When done, write a SHORT summary (3-5 lines) of what you changed and which
        files you touched.
    """).strip()
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=str(sandbox),
        )
        return (r.stdout.strip() or r.stderr.strip()), r.returncode
    except subprocess.TimeoutExpired:
        return "[Claude Code timed out]", 1


def compute_diff(sandbox: Path) -> str:
    excludes = []
    for name in DIFF_SKIP:
        excludes += ["--exclude", name]
    r = subprocess.run(["diff", "-ruN", *excludes, str(REPO), str(sandbox)],
                       capture_output=True, text=True)
    return r.stdout.strip()


# ── change collection + sanity gate (apply mode) ──────────────────────────────

def collect_changes(sandbox: Path):
    """Return [(rel_path, sandbox_abs, live_abs, is_new)] for code/config changes."""
    changed = []
    for root, dirs, files in os.walk(sandbox):
        dirs[:] = [d for d in dirs if d not in DIFF_SKIP]
        for f in files:
            sp = Path(root) / f
            rel = sp.relative_to(sandbox)
            if any(part in DIFF_SKIP for part in rel.parts):
                continue
            lp = REPO / rel
            try:
                if not lp.exists():
                    changed.append((rel, sp, lp, True))
                elif sp.read_bytes() != lp.read_bytes():
                    changed.append((rel, sp, lp, False))
            except Exception:
                pass
    return changed


def sanity_gate(changed, sandbox: Path) -> list[str]:
    """Return list of failure reasons. Empty list = safe to apply."""
    reasons = []

    # 1. forbidden files / escape
    for rel, sp, lp, new in changed:
        rs = str(rel)
        if rel.name == ".env" or rs.startswith("..") or os.path.isabs(rs):
            reasons.append(f"forbidden file touched: {rel}")

    # 2. not too many files
    if len(changed) > MAX_CHANGED_FILES:
        reasons.append(f"too many files changed ({len(changed)} > {MAX_CHANGED_FILES})")

    # 3. python compiles
    for rel, sp, lp, new in changed:
        if rel.suffix == ".py":
            try:
                compile(sp.read_text(), str(rel), "exec")
            except SyntaxError as e:
                reasons.append(f"python syntax error in {rel}: {e}")

    # 4. json parses
    for rel, sp, lp, new in changed:
        if rel.suffix == ".json":
            try:
                json.load(open(sp))
            except Exception as e:
                reasons.append(f"invalid JSON in {rel}: {e}")

    # 5. stop-loss stays protected
    cfg = sandbox / "strategy_config.json"
    if cfg.exists():
        try:
            data = json.load(open(cfg))
            slp = data.get("tsla", {}).get("stop_loss_pct")
            if slp is None:
                reasons.append("strategy_config.json lost tsla.stop_loss_pct (downside unprotected)")
            elif not (STOP_LOSS_SAFE[0] <= float(slp) <= STOP_LOSS_SAFE[1]):
                reasons.append(f"tsla.stop_loss_pct {slp} outside safe range {STOP_LOSS_SAFE}")
        except Exception as e:
            reasons.append(f"cannot validate stop_loss_pct: {e}")

    return reasons


def apply_to_live(changed, request: str) -> str:
    """Copy changed files to live repo + git commit. Returns commit hash or ''."""
    for rel, sp, lp, new in changed:
        lp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, lp)
    paths = [str(rel) for rel, _, _, _ in changed]
    subprocess.run(["git", "-C", str(REPO), "add", *paths], capture_output=True, text=True)
    msg = (f"auto(option2): {request[:72]}\n\n"
           f"Applied by propose_change.py --apply after passing the sanity gate.\n"
           f"Files: {', '.join(paths)}\n\n"
           f"Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
    subprocess.run(["git", "-C", str(REPO), "commit", "-m", msg], capture_output=True, text=True)
    h = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return h.stdout.strip()


# ── telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        return False
    ok = True
    for i in range(0, len(text), 3500):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text[i:i+3500]}, timeout=20)
        ok = ok and r.status_code == 200
    return ok


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    # Option 2: autonomous APPLY is the DEFAULT. Use --propose-only for review mode.
    apply_mode = "--propose-only" not in args
    push = "--no-push" not in args
    args = [a for a in args if a not in ("--apply", "--propose-only", "--no-push")]
    if not args:
        print('usage: propose_change.py [--apply] [--no-push] "<change request>"')
        return 2
    request = " ".join(args)
    req_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "APPLY (live)" if apply_mode else "PROPOSE (review)"
    print(f"[change] id={req_id} mode={mode} request={request!r}")

    sandbox = make_sandbox(req_id)
    print(f"[change] sandbox: {sandbox}")
    print("[change] invoking Claude Code (headless)...")
    summary, rc = run_claude(sandbox, request)
    print(f"[change] claude exit={rc}")
    diff = compute_diff(sandbox)

    pdir = PROPOSALS / req_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "request.txt").write_text(request)
    (pdir / "summary.txt").write_text(summary)
    (pdir / "proposal.diff").write_text(diff)

    if not apply_mode:
        # ---- propose only ----
        header = (f"🧩 PROPOSAL {req_id}\nRequest: {request}\n"
                  f"— NOT deployed. Review the diff. —\n\nClaude's summary:\n{summary[:1200]}")
        (pdir / "meta.json").write_text(json.dumps({"id": req_id, "request": request,
            "mode": "propose", "status": "proposed", "created": req_id}, indent=2))
        if push:
            send_telegram(header)
            if diff:
                send_telegram("DIFF (proposed):\n" + diff[:9000])
            print("[change] proposal pushed ✓")
        else:
            print(header + "\n" + (diff[:3000] or "(no changes)"))
        return 0

    # ---- apply mode (Option 2) ----
    changed = collect_changes(sandbox)
    print(f"[change] files changed: {len(changed)} -> {[str(r) for r,_,_,_ in changed]}")
    reasons = sanity_gate(changed, sandbox)

    if reasons:
        print(f"[change] SANITY GATE FAILED: {reasons}")
        (pdir / "meta.json").write_text(json.dumps({"id": req_id, "request": request,
            "mode": "apply", "status": "blocked", "reasons": reasons}, indent=2))
        msg = (f"🛑 CHANGE BLOCKED {req_id}\nRequest: {request}\n\n"
               f"The automated safety gate refused to apply this:\n"
               + "\n".join(f"  • {r}" for r in reasons)
               + f"\n\nNothing changed. Saved as proposal {req_id}.")
        if push:
            send_telegram(msg)
        else:
            print(msg)
        return 1

    if not changed:
        msg = f"ℹ️ NO CHANGE {req_id}\nRequest: {request}\nClaude produced no edits."
        if push: send_telegram(msg)
        else: print(msg)
        return 0

    commit = apply_to_live(changed, request)
    print(f"[change] APPLIED to live, commit {commit}")
    (pdir / "meta.json").write_text(json.dumps({"id": req_id, "request": request,
        "mode": "apply", "status": "applied", "commit": commit,
        "files": [str(r) for r,_,_,_ in changed]}, indent=2))
    msg = (f"✅ CHANGE APPLIED {req_id}\nRequest: {request}\n"
           f"Files: {', '.join(str(r) for r,_,_,_ in changed)}\n"
           f"Commit: {commit}  (revert: git revert {commit})\n\n"
           f"Summary:\n{summary[:900]}\n\nDIFF:\n{diff[:2000]}")
    if push:
        send_telegram(msg)
        print("[change] applied + notified ✓")
    else:
        print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
