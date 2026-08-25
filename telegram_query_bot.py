"""
telegram_query_bot.py — talk to your Alpaca wallet dataset over Telegram,
answered by the LOCAL Ollama LLM (no cloud calls, $0/query).

Long-polls the same bot that already pushes wallet_report.py's scheduled
reports (TELEGRAM_BOT_TOKEN in .env). Only the chat in TELEGRAM_CHAT_ID may
ask questions — everyone else is ignored, same allowlist model as
Hermes_Telegram_Bridge's TelegramBot.

Each question:
  1. Pulls a fresh snapshot of every configured wallet (compare.gather) plus
     today's fills — the same data wallet_report.py pushes on schedule.
  2. Hands that snapshot + your question to hermes_client.ask() (Ollama,
     gemma4:26b by default) with a strict "use only the data given" system
     prompt — same grounding discipline as analyst_llm.py, to avoid
     the local-model hallucination problem that file already flagged.
  3. Replies in the same Telegram chat.

Run standalone (blocks, long-polling):
    venv/bin/python3 telegram_query_bot.py

One-off CLI test (no Telegram, no polling — validates the data+Ollama path):
    venv/bin/python3 telegram_query_bot.py --once "how are my wallets doing today?"
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

import compare
import hermes_client
import i18n
import wallet_report as wr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [query_bot] %(message)s")
log = logging.getLogger("query_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API = "https://api.telegram.org/bot{token}/{method}"

MODEL = os.environ.get("OLLAMA_QUERY_MODEL", hermes_client.DEFAULT_MODEL)

SYSTEM_PROMPT = """You are a terse financial data assistant for Peter's Alpaca
paper-trading wallets (strategies include copying US-congressional "smart
money" stock disclosures and momentum/swing systems).

You will be given a snapshot of ALL wallets (equity, day P&L, top movers,
today's fills). Answer the user's question using ONLY the facts in that
snapshot.

STRICT RULES:
- Never invent numbers, tickers, news, or events not present in the snapshot.
- If the snapshot doesn't contain what's needed to answer, say so plainly —
  do not guess.
- Plain prose, mobile-friendly. No markdown headers, no long bullet essays
  unless the user's question specifically asks for a list.
- Be direct. Skip filler, skip disclaimers, skip "as an AI".
- Mixed English / Traditional Chinese input is fine — reply in whichever
  language the question was asked in.
"""

GREETINGS = {"hi", "hello", "hey", "start", "menu", "help", "?"}
GREETING_MSG = (
    "Ask me about your Alpaca wallets — e.g. \"how's High Risk doing today?\", "
    "\"what did we trade today?\", \"which wallet is winning?\". "
    f"Answered locally via Ollama ({MODEL}), grounded on live wallet data."
)


def _snapshot_digest() -> str:
    """Same data wallet_report.py pushes on schedule, minus the LLM analyst
    line (we're about to generate our own grounded answer on top of it)."""
    results, fills = wr.gather_report_data()
    return wr.build_wallets_report(results, fills)


def answer(question: str) -> str:
    if not hermes_client.is_alive():
        return ("⚠️ Local Ollama isn't reachable at localhost:11434 — "
                "can't answer right now. Is it running?")
    digest = _snapshot_digest()
    prompt = f"WALLET SNAPSHOT:\n{digest}\n\nQUESTION: {question}"
    reply = hermes_client.ask(prompt, model=MODEL, system=SYSTEM_PROMPT,
                              temperature=0.2, max_tokens=400)
    if reply.startswith("[hermes_client error]"):
        return f"⚠️ {reply}"
    return reply


# ── Telegram long-poll loop ────────────────────────────────────────────────

def _api(method: str, **params):
    r = requests.post(API.format(token=TOKEN, method=method), json=params,
                      timeout=65)
    r.raise_for_status()
    return r.json()


def _send(chat_id, text: str):
    for part in wr.hr._split_for_telegram(text):
        try:
            _api("sendMessage", chat_id=chat_id, text=part)
        except Exception as e:  # noqa: BLE001
            log.error("send failed: %s", e)


def run():
    if not TOKEN:
        log.error("no TELEGRAM_BOT_TOKEN in .env — nothing to poll")
        return
    if not ALLOWED_CHAT_ID:
        log.warning("no TELEGRAM_CHAT_ID set — refusing to run wide open")
        return
    try:
        i18n.set_lang((__import__("json").load(open(_HERE / "strategy_config.json"))
                       .get("tui", {}) or {}).get("language", "en"))
    except Exception:
        pass

    log.info("listening (model=%s, allowed_chat=%s) — Ctrl-C to stop",
             MODEL, ALLOWED_CHAT_ID)
    offset: Optional[int] = None
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            for upd in _api("getUpdates", **params).get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg or "text" not in msg:
                    continue
                chat_id = str(msg["chat"]["id"])
                text = msg["text"].strip()
                if chat_id != str(ALLOWED_CHAT_ID):
                    log.warning("ignoring message from unauthorized chat %s", chat_id)
                    continue
                low = text.lower().lstrip("/")
                if not text or low in GREETINGS:
                    _send(chat_id, GREETING_MSG)
                    continue
                log.info("Q: %s", text)
                reply = answer(text)
                log.info("A: %s", reply[:200])
                _send(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            log.error("poll error: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    if "--once" in sys.argv:
        idx = sys.argv.index("--once")
        q = " ".join(sys.argv[idx + 1:]) or "how are my wallets doing today?"
        print(f"Q: {q}\n")
        print(answer(q))
        sys.exit(0)
    run()
