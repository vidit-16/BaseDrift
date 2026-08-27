"""
PayeeProof — llm_client.py

Single place that talks to the LLM provider. Everything else imports
from here, so a model deprecation is a one-line fix in MODEL_PREFERENCE
rather than a hunt across the codebase.

Provider: Groq (free tier, no credit card).
  Get a key at console.groq.com, then:
    PowerShell:  $env:GROQ_API_KEY="gsk_..."
    cmd:         set GROQ_API_KEY=gsk_...

Handles, because all three actually bit us during development:
  - model deprecation      -> auto-detects a live model from /models
  - rate limiting (429)    -> honours Groq's suggested wait and retries
  - empty content          -> reasoning models can burn the token budget
                              thinking; falls back to the reasoning field
                              and reports finish_reason when truly empty
"""

import json
import os
import re
import time

try:
    import requests
except ImportError:
    raise SystemExit("requests not installed. Run:  pip install requests")


# ── Config ────────────────────────────────────────────────────────────

CHAT_URL   = "https://api.groq.com/openai/v1/chat/completions"
MODELS_URL = "https://api.groq.com/openai/v1/models"

# Tried in order; first one live on the account wins.
# llama-3.3-70b-versatile was shut down 2026-08-16 — do not re-add it.
MODEL_PREFERENCE = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
]

# Free tier is ~8000 tokens/min. Calls run ~900 tokens, so ~9/min.
SECONDS_BETWEEN_CALLS = 7.0

# Reasoning models spend tokens thinking before emitting JSON.
DEFAULT_MAX_TOKENS = 2000

_cached_model = None


def get_api_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def detect_model(api_key=None, force=False):
    """
    Returns (model_id, error). Caches after the first successful call.
    """
    global _cached_model
    if _cached_model and not force:
        return _cached_model, None

    api_key = api_key or get_api_key()
    if not api_key:
        return None, "GROQ_API_KEY is not set"

    try:
        r = requests.get(MODELS_URL,
                         headers={"Authorization": f"Bearer {api_key}"},
                         timeout=20)
        body = r.json()
    except Exception as e:
        return None, f"could not list models: {e}"

    if r.status_code != 200:
        msg = body.get("error", {}).get("message", str(body)[:300])
        return None, f"HTTP {r.status_code} listing models: {msg}"

    available = {m["id"] for m in body.get("data", [])}
    if not available:
        return None, "model list came back empty"

    for preferred in MODEL_PREFERENCE:
        if preferred in available:
            _cached_model = preferred
            return preferred, None

    for m in sorted(available):
        if not any(x in m for x in ("whisper", "guard", "tts", "embed")):
            _cached_model = m
            return m, None

    return None, f"no usable chat model. Available: {sorted(available)}"


def call(system_prompt, user_content, max_tokens=DEFAULT_MAX_TOKENS,
         temperature=0.0, model=None, api_key=None, max_retries=4):
    """
    Returns (text, error). Exactly one is None.
    Retries on 429. Never raises.
    """
    api_key = api_key or get_api_key()
    if not api_key:
        return None, "GROQ_API_KEY is not set"

    if model is None:
        model, err = detect_model(api_key)
        if err:
            return None, err

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(max_retries + 1):
        try:
            r = requests.post(CHAT_URL, headers=headers,
                              data=json.dumps(payload), timeout=45)
        except requests.Timeout:
            return None, "request timed out"
        except Exception as e:
            return None, f"network error: {e}"

        try:
            body = r.json()
        except Exception:
            return None, f"HTTP {r.status_code}, non-JSON: {r.text[:200]}"

        if r.status_code == 200:
            break

        msg = body.get("error", {}).get("message", str(body)[:300])

        if r.status_code == 429 and attempt < max_retries:
            wait = 8.0
            m = re.search(r"try again in ([\d.]+)s", msg)
            if m:
                wait = float(m.group(1)) + 2.0
            time.sleep(wait)
            continue

        return None, f"HTTP {r.status_code}: {msg}"
    else:
        return None, "exhausted retries on rate limit"

    # Extract content, tolerating reasoning-model quirks
    try:
        choice = body["choices"][0]
        msg_obj = choice["message"]
    except (KeyError, IndexError):
        return None, f"unexpected response shape: {str(body)[:300]}"

    text = (msg_obj.get("content") or "").strip()
    if not text:
        text = (msg_obj.get("reasoning") or "").strip()
    if not text:
        finish = choice.get("finish_reason", "?")
        return None, (f"empty content (finish_reason={finish}); "
                      f"raise max_tokens if finish_reason=length")

    return text, None


def call_json(system_prompt, user_content, **kwargs):
    """
    Same as call(), but parses JSON out of the response.
    Returns (dict, error). Tolerates markdown fences and surrounding prose.
    """
    text, err = call(system_prompt, user_content, **kwargs)
    if err:
        return None, err

    cleaned = re.sub(r"^```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # Some models emit prose around the JSON — take the outermost object
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        cleaned = m.group()

    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse failed ({e}): {cleaned[:200]}"


def pace():
    """Sleep between calls to stay under the free-tier rate limit."""
    time.sleep(SECONDS_BETWEEN_CALLS)
