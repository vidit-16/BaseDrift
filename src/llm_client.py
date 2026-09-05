"""
BaseDrift — llm_client.py

Single place that talks to the LLM provider. Everything else imports
from here, so a model deprecation is a one-line fix in MODEL_PREFERENCE
rather than a hunt across the codebase.

THE PROVIDER IS CONFIGURATION, NOT CODE
=======================================
COMPLIANCE.md rests an argument on this file: the pinned model is OPEN-WEIGHT,
and this is the only module IN THE DECISION PATH that talks to a provider, so
bringing inference inside India for RBI payment-data localisation is a one-file
change.

The qualifier matters. eval/ablation.py is deliberately standalone and issues
its own HTTP; it is not part of the decision path, but it reads the same
variables below, because an evaluation that can measure a different provider
than the system runs on is not evidence about the system. That claim
was true but untested. It is now one ENVIRONMENT VARIABLE, and exercised:

  BASEDRIFT_BASE_URL   provider root, OpenAI-compatible   (default: Groq)
  BASEDRIFT_API_KEY    key for that provider              (falls back to
                                                            GROQ_API_KEY)
  BASEDRIFT_MODEL      pin a model id, skipping detection
  BASEDRIFT_PROVIDER   OpenRouter only: the host(s) allowed to serve the
                        model, comma separated and tried in order. NO host
                        outside the list can ever serve a call. List one for a
                        hard pin; list two for a named primary and a named
                        fallback.
  BASEDRIFT_CALL_GAP   seconds between calls (default 7.0, a Groq free-tier
                        figure that costs 90 minutes anywhere else)

The model does not change when the provider does. gpt-oss-120b is open-weight,
so a dozen companies run the same weights; moving between them keeps both the
localisation argument and comparability with v1's measurements, which a switch
to a closed hosted model would forfeit.

  Groq        https://api.groq.com/openai/v1     openai/gpt-oss-120b
  OpenRouter  https://openrouter.ai/api/v1       openai/gpt-oss-120b
  Cerebras    https://api.cerebras.ai/v1         gpt-oss-120b

    PowerShell:  $env:BASEDRIFT_API_KEY="sk-or-..."
                 $env:BASEDRIFT_BASE_URL="https://openrouter.ai/api/v1"

WHY THE SERVING PROVIDER GOES IN THE AUDIT RECORD
=================================================
OpenRouter routes one model id across ~18 hosts, so consecutive calls can be
served by different companies. For most projects that is a feature. Here the
whole point of the audit trail is that a payment decision traces to exactly what
produced it, and "some host in a pool" is a weaker record than a named one. So
the provider is pinned when BASEDRIFT_PROVIDER is set, and whichever host
actually served the call is reported back through `meta` alongside the model.

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

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


def base_url():
    return os.environ.get("BASEDRIFT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def chat_url():
    return f"{base_url()}/chat/completions"


def models_url():
    return f"{base_url()}/models"


def provider_name():
    """A readable host name for the audit record, derived from the base URL."""
    host = base_url().split("//")[-1].split("/")[0]
    return host.replace("api.", "").replace(".com", "").replace(".ai", "")


# Tried in order; first one live on the account wins. Both spellings of the
# same open-weight model are listed: Groq and OpenRouter publish it as
# openai/gpt-oss-120b, Cerebras drops the prefix. Same weights either way.
# llama-3.3-70b-versatile was shut down 2026-08-16 — do not re-add it.
MODEL_PREFERENCE = [
    "openai/gpt-oss-120b",
    "gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
]

# 7.0 is a GROQ FREE TIER figure — 8000 tokens/min against ~900-token calls.
# It is wrong everywhere else and costs 93 minutes over an 800-case run for
# nothing, so it is configurable rather than a constant someone has to find.
DEFAULT_CALL_GAP = 7.0

# Reasoning models spend tokens thinking before emitting JSON. MEASURED on
# gpt-oss-120b: 483 reasoning tokens before the first JSON character, 644
# completion tokens in total. 700 returns HTTP 400 — the response truncates
# mid-object and fails to parse. Do not lower this without measuring again.
DEFAULT_MAX_TOKENS = 2000

_cached_model = None


def get_api_key():
    """
    BASEDRIFT_API_KEY first, GROQ_API_KEY second.

    The fallback is deliberate: an existing Groq setup keeps working untouched,
    so switching provider is additive rather than a migration.
    """
    return (os.environ.get("BASEDRIFT_API_KEY", "").strip()
            or os.environ.get("GROQ_API_KEY", "").strip())


def call_gap():
    try:
        return float(os.environ.get("BASEDRIFT_CALL_GAP", DEFAULT_CALL_GAP))
    except ValueError:
        return DEFAULT_CALL_GAP


def detect_model(api_key=None, force=False):
    """
    Returns (model_id, error). Caches after the first successful call.
    """
    global _cached_model
    if _cached_model and not force:
        return _cached_model, None

    pinned = os.environ.get("BASEDRIFT_MODEL", "").strip()
    if pinned:
        _cached_model = pinned
        return pinned, None

    api_key = api_key or get_api_key()
    if not api_key:
        return None, "no API key: set BASEDRIFT_API_KEY (or GROQ_API_KEY)"

    try:
        r = requests.get(models_url(),
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
         temperature=0.0, model=None, api_key=None, max_retries=4, meta=None):
    """
    Returns (text, error). Exactly one is None.
    Retries on 429. Never raises.

    `meta`, if given, is filled with the model that actually served the call.
    MODEL_PREFERENCE auto-detects, so the caller frequently does not know which
    model produced a result — and an audit record for a payment decision has to
    say which one did. Passing a dict rather than changing the return arity
    keeps every existing caller and test stub working.
    """
    api_key = api_key or get_api_key()
    if not api_key:
        return None, "no API key: set BASEDRIFT_API_KEY (or GROQ_API_KEY)"

    if model is None:
        model, err = detect_model(api_key)
        if err:
            return None, err
    if meta is not None:
        meta["model"] = model
        meta["provider"] = provider_name()

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
    }

    # OpenRouter routes one model id across many hosts, so consecutive calls can
    # be served by different companies. Pinning makes the audit record name one.
    # Ignored by providers that do not understand the field.
    #
    # ORDER, not `only`. The first version used {"only": [host],
    # "allow_fallbacks": False}, which turned that one host's rate limit into a
    # failed extraction: pinned to a busy provider the call returned HTTP 429,
    # R1 fired, and a legitimate payout was held. Unpinned, the identical
    # request succeeded elsewhere seconds later.
    #
    # That traded availability for auditability unconditionally, and it did not
    # need to. An ORDER over several named hosts keeps both: the call falls
    # through to the next host on the list, and served_by names whoever ran it.
    #
    # allow_fallbacks STAYS FALSE. `order` alone would let OpenRouter fall
    # through to a host that is NOT on the list once the named ones are
    # exhausted — an unnamed, unassessed processor handling payment data, which
    # is the thing the pin exists to prevent. Every host that can serve a call
    # must be one somebody put in the list on purpose.
    #
    # So the knob is how MANY named hosts, not whether an unnamed one may step
    # in. One host is the strict pin; two is a named primary and a named
    # fallback; the failure mode with a single busy host is what prompted this.
    pinned = [h.strip() for h in
              os.environ.get("BASEDRIFT_PROVIDER", "").split(",") if h.strip()]
    if pinned:
        payload["provider"] = {"order": pinned,
                               "only": pinned,
                               "allow_fallbacks": False}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(max_retries + 1):
        try:
            r = requests.post(chat_url(), headers=headers,
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

    # Which host actually served this. OpenRouter reports it; others do not,
    # in which case the base URL is the honest answer. It belongs in the audit
    # record for the same reason the model id does.
    if meta is not None:
        meta["served_by"] = body.get("provider") or provider_name()
        usage = body.get("usage") or {}
        if usage:
            meta["usage"] = {k: usage[k] for k in
                             ("prompt_tokens", "completion_tokens", "total_tokens")
                             if k in usage}

    text = (msg_obj.get("content") or "").strip()
    if not text:
        text = (msg_obj.get("reasoning") or "").strip()
    if not text:
        finish = choice.get("finish_reason", "?")
        return None, (f"empty content (finish_reason={finish}); "
                      f"raise max_tokens if finish_reason=length")

    return text, None


def _extract_json(text):
    """(parsed, error). Tolerates markdown fences and surrounding prose."""
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


JSON_ATTEMPTS = 2


def call_json(system_prompt, user_content, attempts=JSON_ATTEMPTS, **kwargs):
    """
    Same as call(), but parses JSON out of the response.
    Returns (dict, error). Accepts and forwards `meta` — see call().

    RESAMPLES ONCE ON A PARSE FAILURE, AND DOES NOT REPAIR THE JSON.

    Measured over 900 extractions: 8 responses (0.9%) were malformed — five
    "Expecting ',' delimiter", two "Expecting value", one "Invalid control
    character". The dominant class is almost certainly an unescaped quote
    inside urgency_phrases or channel_manipulation_phrases, which quote the
    email back, and email text contains quotes.

    Repairing that is guesswork: with an unescaped quote you cannot tell where
    the string was meant to end, so a "fix" can silently change what the model
    said about a payment. Evidence that has been quietly rewritten is worse
    than evidence that is missing, because the audit record stops being a
    record. Resampling asks the same question again and takes an answer that
    parses — the output is non-deterministic, so a second sample usually does.

    A parse failure is still a failure if the retry also fails. It resolves to
    R1_extraction_failed and the payout is held, which is the safe state.
    """
    last = None
    for _ in range(max(1, attempts)):
        text, err = call(system_prompt, user_content, **kwargs)
        if err:
            return None, err
        parsed, perr = _extract_json(text)
        if perr is None:
            return parsed, None
        last = perr
    return None, last


def pace():
    """
    Sleep between calls to stay under the provider's rate limit.

    The default of 7s is Groq's free tier and nothing else. On a provider with
    no per-minute ceiling, set BASEDRIFT_CALL_GAP=0.1 — over an 800-case run
    the default alone costs 93 minutes.
    """
    gap = call_gap()
    if gap > 0:
        time.sleep(gap)
