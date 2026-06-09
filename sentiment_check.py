"""
sentiment_check.py — news-based sentiment overlay for incoming trades.

For a given ticker:
  1. Fetch recent news headlines (Yahoo Finance RSS — no API key needed)
  2. Ask local LLM (gemma4) for sentiment 1–5 + flag any red flags
  3. Return a size multiplier 0.5x..1.5x for capitol_copier to apply

Cached for 6 hours per ticker (recent enough to be relevant, long enough
to avoid hammering the news feed on consecutive scans).

Used by:
  - capitol_copier.py copy_trade() — applies multiplier before placing buys
"""

import os, json, re, requests
from datetime import datetime, timezone, timedelta
import hermes_client as llm

CACHE_FILE = os.path.join(os.path.dirname(__file__), ".sentiment_cache.json")
CACHE_TTL_HOURS = 6

# Multipliers per LLM-scored sentiment
MULTIPLIER_BY_SCORE = {
    1: 0.50,   # very negative — half-size
    2: 0.75,
    3: 1.00,   # neutral — no change
    4: 1.25,
    5: 1.50,   # very positive — boost
}


def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            try:    return json.load(f)
            except: pass
    return {}


def _save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _is_fresh(timestamp_iso):
    if not timestamp_iso:
        return False
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - ts
    return age < timedelta(hours=CACHE_TTL_HOURS)


# ── News fetching ────────────────────────────────────────────────────────────

def fetch_headlines(ticker, max_headlines=10):
    """Fetch recent headlines for a ticker. Returns list of {title, summary, date}."""
    headlines = []

    # Try Yahoo Finance RSS (free, no key needed)
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        r = requests.get(url,
                         headers={"User-Agent": "Mozilla/5.0"},
                         timeout=10)
        if r.status_code == 200:
            # Simple RSS parsing
            items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
            for item in items[:max_headlines]:
                title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                desc_m  = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
                pub_m   = re.search(r"<pubDate>(.*?)</pubDate>", item)
                title = title_m.group(1).strip() if title_m else ""
                desc  = desc_m.group(1).strip() if desc_m else ""
                # Strip CDATA
                title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title)
                desc  = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc)
                # Strip HTML
                title = re.sub(r"<[^>]+>", "", title)
                desc  = re.sub(r"<[^>]+>", "", desc)
                if title:
                    headlines.append({
                        "title": title[:200],
                        "summary": desc[:300],
                        "date": pub_m.group(1) if pub_m else "",
                    })
    except Exception as e:
        print(f"[sentiment] Yahoo fetch error for {ticker}: {e}")

    return headlines


# ── LLM sentiment scoring ────────────────────────────────────────────────────

def score_sentiment(ticker, headlines):
    """Ask local LLM to score sentiment 1-5 and flag any concerns."""
    if not headlines:
        return {
            "score":      3,
            "multiplier": 1.0,
            "flag":       "no recent news",
            "reasoning":  "no headlines found",
            "n_headlines": 0,
        }

    headlines_text = "\n".join(
        f"  {i+1}. {h['title']}"
        + (f" — {h['summary'][:120]}" if h.get('summary') else "")
        for i, h in enumerate(headlines)
    )

    prompt = f"""You are evaluating recent news about the stock ticker {ticker} for a smart-money
trade-copying strategy. Recent headlines:

{headlines_text}

Score the OVERALL sentiment 1 to 5:
  1 = very bearish (lawsuits, earnings miss, downgrades, scandal)
  2 = bearish (caution, weak guidance)
  3 = neutral (mixed or no clear bias)
  4 = bullish (positive guidance, upgrades, partnerships)
  5 = very bullish (blowout earnings, major catalysts)

Also flag any ACTIONABLE risks in a single phrase (e.g., "earnings tomorrow",
"FDA decision pending", "downgraded by 2 analysts").

Respond ONLY in this exact JSON format on one line:
{{"score": <1-5>, "flag": "<short flag or empty string>", "reasoning": "<one sentence>"}}
"""

    response = llm.ask(prompt, model="hermes3:8b", temperature=0.2,
                       max_tokens=200, timeout=60)

    # Parse response
    try:
        json_match = re.search(r"\{[^}]+\}", response)
        if json_match:
            data = json.loads(json_match.group(0))
            score = int(data.get("score", 3))
            score = max(1, min(5, score))
            return {
                "score":      score,
                "multiplier": MULTIPLIER_BY_SCORE.get(score, 1.0),
                "flag":       str(data.get("flag", ""))[:80],
                "reasoning":  str(data.get("reasoning", ""))[:200],
                "n_headlines": len(headlines),
            }
    except Exception as e:
        print(f"[sentiment] parse error for {ticker}: {e}")

    # Fallback if LLM didn't return valid JSON
    return {
        "score":      3,
        "multiplier": 1.0,
        "flag":       "",
        "reasoning":  f"parse failed; raw: {response[:100]}",
        "n_headlines": len(headlines),
    }


# ── Public API ───────────────────────────────────────────────────────────────

def get_sentiment(ticker):
    """Return cached or fresh sentiment for a ticker."""
    cache = _load_cache()
    cached = cache.get(ticker)
    if cached and _is_fresh(cached.get("checked_at")):
        return cached

    if not llm.is_alive():
        # Fallback: neutral if Ollama is down
        return {
            "score":      3,
            "multiplier": 1.0,
            "flag":       "LLM unreachable",
            "reasoning":  "Ollama not running, defaulting to neutral",
            "n_headlines": 0,
        }

    headlines = fetch_headlines(ticker)
    result = score_sentiment(ticker, headlines)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()

    cache[ticker] = result
    _save_cache(cache)
    return result


def summary_line(sentiment):
    """One-line printable summary."""
    arrow = "↑↑" if sentiment["multiplier"] > 1.2 else (
            "↓↓" if sentiment["multiplier"] < 0.8 else "→")
    flag = f"  ⚠️ {sentiment['flag']}" if sentiment.get("flag") else ""
    return f"sentiment {sentiment['score']}/5 {arrow} x{sentiment['multiplier']:.2f}{flag}"


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "GOOGL"
    print(f"Checking sentiment for {ticker}...\n")
    s = get_sentiment(ticker)
    print(json.dumps(s, indent=2))
