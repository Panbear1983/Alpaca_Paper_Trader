"""
openrouter_analyst.py — LLM analyst summary via OpenRouter.

Takes the fully-composed report text and returns ONE grounded analyst paragraph.
Used by hermes_report.py to append a closing "Analyst Take" section.

Why OpenRouter (not local Ollama): the local hermes3:8b fabricated news and
mis-copied numbers. A frontier model via OpenRouter, with a strict "use only the
data provided" system prompt, gives grounded analysis without hallucination.

Setup:
  1. Get a key at https://openrouter.ai/keys
  2. Add to .env in this folder:
       OPENROUTER_API_KEY=sk-or-...
       OPENROUTER_MODEL=anthropic/claude-3.7-sonnet     (optional override)

If no key is set, summarize() returns None and the report omits the section.

Cost: this runs once per trading day. A Claude-Sonnet call on a ~2KB report is
roughly US$0.01–0.02. ~$0.30/month.
"""

import os
from pathlib import Path
import requests
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
load_dotenv(Path.home() / ".hermes" / ".env", override=False)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
MODEL   = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6").strip()
URL     = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """You are a sharp, grounded financial analyst reviewing a paper-trading
account that copies US-congressional ("smart money") stock disclosures plus a
TSLA trailing-stop strategy.

You will receive a structured daily report (smart-money targets + account stats).
Write EXACTLY ONE tight paragraph (4–6 sentences, under 150 words) to close the report.
Lead with the single most important takeaway. Finish your final sentence — never trail off.

STRICT RULES:
- Use ONLY facts present in the report. NEVER invent news, prices, tickers,
  macro events, earnings, or geopolitics. If it is not in the report, do not mention it.
- No markdown, no headers, no bullet lists — plain prose only.
- Be specific and useful: reference the actual biggest movers, the pool's posture
  (who we follow, any on probation), cash deployment, and alpha vs SPY when present.
- Professional and direct. No filler, no hedging disclaimers, no "as an AI".
"""


def is_configured() -> bool:
    return bool(API_KEY)


def summarize(report_text: str, model: str | None = None) -> str | None:
    """Return one analyst paragraph, or None if no API key configured.

    On API error, returns a short '[analyst error: …]' string so the caller can
    decide whether to show a fallback line.
    """
    if not API_KEY:
        return None

    try:
        r = requests.post(
            URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/Panbear1983/Alpaca_Paper_Trader",
                "X-Title":       "Alpaca Paper Trader",
            },
            json={
                "model": model or MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user",   "content": report_text},
                ],
                "max_tokens":  600,
                "temperature": 0.4,
            },
            timeout=60,
        )
        if r.status_code != 200:
            return f"[analyst error: HTTP {r.status_code} {r.text[:120]}]"
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[analyst error: {type(e).__name__}: {e}]"


if __name__ == "__main__":
    print(f"OpenRouter configured: {is_configured()}")
    print(f"Model: {MODEL}")
    if is_configured():
        sample = ("*Smart Money Pool*\nFollowing: Blumenthal, McCaul, Gottheimer.\n"
                  "*Account*\nEquity $99,900, Day P&L +$150 (+0.15%), SPY -0.8%.\n"
                  "Top movers: MRVL +42%, MU +20%. Worst: INTC -17%, SBUX -11%.")
        print("\nTest summary:\n" + str(summarize(sample)))
