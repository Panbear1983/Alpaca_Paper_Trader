"""
hermes_client.py — local LLM wrapper around Ollama.

Talks to Ollama's HTTP API at localhost:11434 to call gemma4:26b (or any local
model). Used by daily_briefing.py, sentiment_check.py, and future Hermes-driven
features.

Why a wrapper:
  - Centralize the model choice (we can swap gemma4 → llama3 → qwen3 in one place)
  - Add retry / timeout / error handling
  - Provide a simple `ask(prompt)` interface that returns a string

Local LLM cost: $0 per call. Latency: 5–30s on gemma4:26b depending on prompt size.
"""

import json
import requests

OLLAMA_URL    = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b"      # best local reasoning we have
FAST_MODEL    = "gemma4:e4b"      # smaller / faster fallback (8B)
TINY_MODEL    = "hermes3:8b"      # tiny model for one-liners


def ask(prompt, *, model=DEFAULT_MODEL, system=None, temperature=0.3,
        max_tokens=1024, timeout=300):
    """Ask the local LLM a question. Returns the response text (string)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":      model,
                "messages":   messages,
                "stream":     False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return f"[hermes_client error] HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        msg = data.get("message", {}).get("content", "")
        return msg.strip()
    except requests.Timeout:
        return "[hermes_client error] timeout waiting for local LLM"
    except Exception as e:
        return f"[hermes_client error] {e}"


def is_alive():
    """Check if Ollama is running and reachable."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def available_models():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


if __name__ == "__main__":
    print(f"Ollama alive: {is_alive()}")
    print(f"Models: {available_models()}")
    if is_alive():
        print(f"\nTesting with {DEFAULT_MODEL}...")
        reply = ask("In one sentence: what makes a good trading strategy?",
                    max_tokens=120)
        print(f"\nReply:\n{reply}")
