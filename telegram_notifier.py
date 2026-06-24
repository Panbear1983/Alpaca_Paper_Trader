"""
telegram_notifier.py — Telegram Bot API wrapper for Claude Trader alerts.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env (or env vars).
Sends formatted messages to your iOS device via Telegram's native push.

Used by:
  - event_watcher.py     — real-time trade / pool / fill notifications
  - daily_briefing.py    — morning AI summary
  - sentiment_check.py   — sentiment warnings on incoming trades
  - capitol_copier.py    — per-trade confirmation messages
"""

import os, json, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://api.telegram.org"


def _is_configured():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send(message, parse_mode="Markdown", silent=False):
    """Send a single message to your Telegram. Returns True on success."""
    if not _is_configured():
        # Silent fail when no token — useful during dev
        return False

    url = f"{API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


# ── Formatted message helpers ────────────────────────────────────────────────

def notify_batch(title, lines, emoji="📊"):
    """Send ONE consolidated message from a list of action lines, instead of a
    separate push per trade. Chunks on line boundaries if it exceeds Telegram's
    4096-char limit. No-op (returns False) when there are no lines."""
    if not lines:
        return False
    header = f"{emoji} *{title}*  ({len(lines)})"
    full = header + "\n" + "\n".join(lines)
    if len(full) <= 3900:
        return send(full)
    # too long — split across messages on line boundaries
    ok = True
    chunk, clen = [header], len(header)
    for ln in lines:
        if clen + len(ln) + 1 > 3900:
            ok = send("\n".join(chunk)) and ok
            chunk, clen = [header], len(header)
        chunk.append(ln)
        clen += len(ln) + 1
    if len(chunk) > 1:
        ok = send("\n".join(chunk)) and ok
    return ok



def notify_trade(politician_id, ticker, side, size_usd, sentiment=None, consensus=None):
    """Format and send a trade execution notification."""
    emoji = "🟢" if side == "buy" else "🔴"
    msg = f"{emoji} *Trade copied*\n"
    msg += f"`{side.upper():<4}` *{ticker}*  ${size_usd:.0f}\n"
    msg += f"Source: `{politician_id}`"
    if consensus and consensus.get("is_consensus"):
        msg += f"  ⚡️ *CONSENSUS x{consensus['multiplier']}* ({consensus['n_members']} members)"
    if sentiment:
        score = sentiment.get("score", "?")
        flag  = sentiment.get("flag", "")
        msg += f"\nSentiment: {score}{' ⚠️ ' + flag if flag else ''}"
    return send(msg)


def notify_pool_change(added, removed, reason="re-vet"):
    """Pool composition change notification."""
    msg = f"🔄 *Pool {reason}*\n"
    if added:
        msg += f"+ Added: `{', '.join(added)}`\n"
    if removed:
        msg += f"- Removed: `{', '.join(removed)}`"
    return send(msg)


def notify_pool_rebalance(swaps):
    """Pool weight rebalance summary (weekly)."""
    if not swaps:
        return send("⚖️ *Pool weekly rebalance*\nNo significant rank changes")
    msg = "⚖️ *Pool weekly rebalance*\n"
    for s in swaps:
        msg += f"`{s['id']}`  {s['old_weight']*100:.0f}% → {s['new_weight']*100:.0f}%\n"
    return send(msg)


def notify_graduation(promoted, demoted, weeks):
    msg = (f"🎓 *Graduation*\n"
           f"`{promoted}` promoted (after {weeks}w on probation)\n"
           f"`{demoted}` demoted to probation")
    return send(msg)


def notify_stop_hit(ticker, stop_price, qty):
    msg = (f"🛑 *STOP TRIGGERED*\n"
           f"*{ticker}* — sold {qty} shares at ${stop_price}")
    return send(msg)


def notify_position_alert(ticker, message, severity="info"):
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "ℹ️")
    return send(f"{emoji} *{ticker}* — {message}")


def notify_daily_briefing(briefing_text):
    """Send the morning AI briefing as a single message."""
    header = f"📊 *Daily Briefing* · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
    return send(header + briefing_text)


def notify_error(scope, error):
    return send(f"❌ *Error in {scope}*\n```\n{str(error)[:500]}\n```")


# ── CLI for testing ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if not _is_configured():
        print("⚠️  TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set in .env")
        print("    Add them to /Users/peter/Desktop/Old_Projects/GitHub/Alpaca_Paper_Trader/.env:")
        print("      TELEGRAM_BOT_TOKEN=...")
        print("      TELEGRAM_CHAT_ID=...")
        sys.exit(1)

    msg = sys.argv[1] if len(sys.argv) > 1 else "✅ Alpaca Paper Trader Telegram link is live"
    ok = send(msg)
    print(f"Send: {'OK' if ok else 'FAILED'}")
