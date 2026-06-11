#!/usr/bin/env python3
"""
apply_change.py — Option 2 autonomous live-edit entry point.

Thin wrapper that ALWAYS runs propose_change in --apply mode. The script NAME
encodes the mode, so there is no flag for the calling agent (gemini) to drop.
The request is everything after the script name.

Flow (same safety as propose_change --apply):
  sandbox → Claude Code edits → automated sanity gate → if pass: apply to live
  + git commit (revertible) + Telegram "✅ APPLIED"; if fail: "🛑 BLOCKED", nothing changes.

Usage:
  python3 apply_change.py "set the TSLA stop to 9 percent"
  python3 apply_change.py --no-push "..."     # apply but skip telegram (testing)
"""
import sys
import propose_change

if __name__ == "__main__":
    # Force --apply, preserve everything else (request, optional --no-push)
    args = sys.argv[1:]
    if "--apply" not in args:
        args = ["--apply"] + args
    sys.argv = [sys.argv[0]] + args
    raise SystemExit(propose_change.main())
