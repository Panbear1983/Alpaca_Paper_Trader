"""
analyst_llm.py — LLM analyst summary via Hermes.

Takes the fully-composed report text and returns ONE grounded analyst paragraph.
Used by wallet_report.py / hermes_report.py to append a closing "Analyst Take".

Routing (see ROUTES below — first entry is primary, edit the order to change it):
  1. PRIMARY   NVIDIA Nemotron 120B — nvidia/nemotron-3-super-120b-a12b
  2. FALLBACK  Codex "Terra"        — gpt-5.6-terra via provider openai-codex

Both are reached through the Hermes CLI in one-shot mode (`hermes -z`), so:
  - NO OpenRouter, and no OPENROUTER_API_KEY anywhere on the report path
  - NO API key lives in this repo — credentials stay in the Hermes profile
    (Codex is OAuth via `hermes auth`; the NVIDIA key is in the profile .env)

Why a subprocess and not an HTTP call: Codex access is OAuth-bearer held by
Hermes, and re-implementing that token dance here would drift the moment Hermes
refreshes it. `hermes -z` prints ONLY the model's reply on stdout.

If Hermes is missing, summarize() returns None and the report omits the section —
same contract the OpenRouter client had when no key was set.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent

PROFILE = os.environ.get("ALPACA_ANALYST_PROFILE", "wanna_buffet").strip()

# Ordered routing — the FIRST entry is primary, the rest are fallbacks tried in
# order. To change precedence, reorder this list; nothing else needs editing.
#
# provider=None, model=None means "use the profile's own default model", which
# is how NVIDIA Nemotron 120B is reached (wanna_buffet config.yaml sets it).
# Expressing it that way keeps the route correct if the profile is repointed.
ROUTES = [
    (None,           None,             "nvidia/nemotron-3-super-120b-a12b"),
    ("openai-codex", "gpt-5.6-terra",  "gpt-5.6-terra"),
]

# Override the PRIMARY model only (mainly for testing a forced failure).
_OVERRIDE = os.environ.get("ALPACA_ANALYST_MODEL", "").strip()
if _OVERRIDE:
    ROUTES[0] = (ROUTES[0][0], _OVERRIDE, _OVERRIDE)

TIMEOUT_SECONDS = int(os.environ.get("ALPACA_ANALYST_TIMEOUT", "180"))

# Updated by ask() to whichever model actually answered, so callers that log
# `analyst_llm.MODEL` after the call report the truth.
MODEL = ROUTES[0][2]


def _hermes_cmd() -> list[str] | None:
    """Absolute path first — launchd jobs do not get ~/.local/bin on PATH."""
    direct = Path.home() / ".local" / "bin" / "hermes"
    if direct.is_file() and os.access(direct, os.X_OK):
        return [str(direct)]
    found = shutil.which("hermes")
    if found:
        return [found]
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if venv_py.is_file():
        return [str(venv_py), "-m", "hermes_cli.main"]
    return None


SYSTEM = """You are a sharp, grounded financial analyst reviewing one or more
paper-trading wallets (strategies include copying US-congressional "smart money"
stock disclosures and momentum/swing systems).

You will receive a structured daily report (per-wallet stats, holdings, trades).
Write EXACTLY ONE tight paragraph (4-6 sentences, under 150 words) to close the report.
Lead with the single most important takeaway. Finish your final sentence — never trail off.

STRICT RULES:
- Use ONLY facts present in the report. NEVER invent news, prices, tickers,
  macro events, earnings, or geopolitics. If it is not in the report, do not mention it.
- No markdown, no headers, no bullet lists — plain prose only.
- Be specific and useful: reference the actual biggest movers, the pool's posture
  (who we follow, any on probation), cash deployment, and alpha vs SPY when present.
- Professional and direct. No filler, no hedging disclaimers, no "as an AI".
- Output ONLY the paragraph. No preamble, no sign-off.
"""


def is_configured() -> bool:
    """True when the Hermes CLI is reachable. No API key needed in this repo."""
    return _hermes_cmd() is not None


# `hermes -z` exits 0 and prints provider errors to STDOUT (e.g. an unsupported
# model returns `HTTP 400: {"detail": ...}`), so a zero exit code alone does not
# mean success — sniff the payload too, or the fallback never fires.
_ERROR_RE = re.compile(r'^\s*(HTTP\s+\d{3}\b|\{"(detail|error)"|(Error|error)\s*:)')


def _looks_like_error(out: str) -> bool:
    """Providers disagree on error shape, so match several.

    Seen in the wild, all with returncode 0:
      Codex  -> 'HTTP 400: {"detail": "... model is not supported ..."}'
      NVIDIA -> 'API call failed after 3 retries: HTTP 404: 404 page not found'
    """
    head = out[:200]
    return (
        bool(_ERROR_RE.match(out))
        or '"detail":' in head
        or '"error":' in head
        or 'API call failed' in head
        or bool(re.search(r'\bHTTP\s+[45]\d{2}\b', head))
    )


def _run(base: list[str], provider: str | None, model: str | None,
         prompt: str) -> tuple[bool, str]:
    cmd = list(base) + ["--profile", PROFILE, "--reasoning", "minimal"]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["-m", model]
    cmd += ["-z", prompt]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {TIMEOUT_SECONDS}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return False, (p.stderr or "").strip()[:160] or f"exit {p.returncode}"
    if not out:
        return False, "empty response"
    if _looks_like_error(out):
        return False, out[:160]
    return True, out


def ask(prompt: str) -> tuple[bool, str]:
    """Send one prompt down ROUTES in order. Returns (ok, text).

    Sets module-level MODEL to whichever route answered. On total failure the
    text is a short 'route: reason | route: reason' trail.
    """
    global MODEL
    base = _hermes_cmd()
    if base is None:
        return False, "hermes CLI not reachable"
    errs = []
    for provider, model, label in ROUTES:
        ok, out = _run(base, provider, model, prompt)
        if ok:
            MODEL = label
            return True, out
        errs.append(f"{label}: {out[:70]}")
    MODEL = ROUTES[0][2]
    return False, " | ".join(errs)


def summarize(report_text: str, model: str | None = None,
              lang: str = "en") -> str | None:
    """Return one analyst paragraph, or None if Hermes is unavailable.

    Tries Codex Terra first, then falls back to NVIDIA Nemotron 120B. On total
    failure returns a short '[analyst error: ...]' string so the caller can
    decide whether to show a fallback line. lang="zh_TW" asks for Traditional
    Chinese (default stays English for existing callers).
    """
    if _hermes_cmd() is None:
        return None

    system = SYSTEM
    if lang == "zh_TW":
        system += "\n- Write the paragraph in Traditional Chinese (繁體中文)."

    prompt = f"{system}\n\n--- REPORT ---\n{report_text}\n--- END REPORT ---\n"
    ok, out = ask(prompt)
    return out if ok else f"[analyst error: {out}]"


def translate_zh(text: str) -> str:
    """Translate a company description EN -> Traditional Chinese (Taiwan usage).

    Same Terra-then-Nemotron routing as summarize(). Returns '' on any failure
    so the caller silently keeps the English text. The caller caches per symbol.
    """
    if _hermes_cmd() is None or not text.strip():
        return ""
    prompt = (
        "Translate the company description below into Traditional Chinese "
        "(\u7e41\u9ad4\u4e2d\u6587, Taiwan usage). Keep company and product names in "
        "English. Output ONLY the translation, nothing else.\n\n"
        f"--- TEXT ---\n{text[:4000]}\n--- END TEXT ---\n"
    )
    ok, out = ask(prompt)
    return out if ok else ""


if __name__ == "__main__":
    print(f"Hermes reachable : {is_configured()}")
    print(f"Profile          : {PROFILE}")
    for i, (prov, mdl, label) in enumerate(ROUTES):
        role = "Primary " if i == 0 else f"Fallback{i}"
        print(f"{role}         : {label}" + (f" (provider {prov})" if prov else " (profile default)"))
    if is_configured():
        sample = ("*Smart Money Pool*\nFollowing: Blumenthal, McCaul, Gottheimer.\n"
                  "*Account*\nEquity $99,900, Day P&L +$150 (+0.15%), SPY -0.8%.\n"
                  "Top movers: MRVL +42%, MU +20%. Worst: INTC -17%, SBUX -11%.")
        print("\nTest summary:\n" + str(summarize(sample)))
        print(f"\nAnswered by: {MODEL}")
