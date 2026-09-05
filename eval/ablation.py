"""
BaseDrift — semantic ablation test.

Proves the LLM layer does semantic normalization, not entity extraction.

Run:
    pip install requests
    $env:BASEDRIFT_API_KEY="sk-..."       (PowerShell)
    $env:BASEDRIFT_BASE_URL="https://openrouter.ai/api/v1"
  An existing GROQ_API_KEY is still read.
    python ablation.py

This file is self-contained — no imports from the repo — but it honours the
same BASEDRIFT_* variables as src/llm_client.py, so it cannot end up measuring
a different provider than the system runs on.
It auto-detects a working Groq model, so model deprecations won't break it.
"""

import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run:  pip install requests")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

# Deliberately standalone — this file reproduces the ablation without importing
# the rest of the repo. But standalone must not mean DIFFERENTLY CONFIGURED: it
# read a hardcoded Groq URL while the system had moved to another provider, so
# the two could silently disagree about which model was being measured. Same
# variables, same defaults, no import.
API_KEY  = (os.environ.get("BASEDRIFT_API_KEY", "").strip()
            or os.environ.get("GROQ_API_KEY", "").strip())
BASE_URL = os.environ.get("BASEDRIFT_BASE_URL",
                          "https://api.groq.com/openai/v1").rstrip("/")
try:
    CALL_GAP = float(os.environ.get("BASEDRIFT_CALL_GAP", 7.0))
except ValueError:
    CALL_GAP = 7.0
CHAT_URL     = f"{BASE_URL}/chat/completions"
MODELS_URL   = f"{BASE_URL}/models"

# Tried in order. First one available on the account is used.
# Updated Aug 2026: llama-3.3-70b-versatile was shut down Aug 16 2026.
MODEL_PREFERENCE = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
]


# ══════════════════════════════════════════════════════════════════════
# CORPUS — cases where the answer requires inference, not keywords
#
# GROUP A: replace existing destination, covers outstanding + future
# GROUP B: add an account, existing one keeps a role, future only
# GROUP C: control — account numbers and change vocabulary present,
#          but NO change to this vendor's destination
#
# No case states "replace", "add", "update", or any scope keyword.
# ══════════════════════════════════════════════════════════════════════

CORPUS = {
    "A_REPLACE": {
        "expected": ("BENEFICIARY_CHANGE", "REPLACE_PAYOUT_DESTINATION",
                     "OUTSTANDING_AND_FUTURE"),
        "cases": [
            ("A1", "inference via finality",
             "Our treasury has consolidated everything into a single facility. "
             "Going by our records, INV-4471 from October is still open, and we "
             "have the Q1 retainer running through March. Everything should reach "
             "446168753919, PUNB0562290. The HDFC facility closes on the 30th."),

            ("A2", "inference via closure",
             "Quick note from our side - the account you have on file was with our "
             "previous banking partner, whose relationship with us ended last week. "
             "For INV-4471 which is still unpaid, and for the monthly billing that "
             "continues, please use 446168753919 (PUNB0562290)."),

            ("A3", "inference via negative space",
             "We've restructured our banking. 446168753919, PUNB0562290 is where we "
             "now receive everything. INV-4471 is outstanding and the retainer "
             "invoices will keep coming monthly - both of those go here."),

            ("A4", "inference conversational",
             "Hey! So our bank situation shifted - long story. 446168753919 "
             "(PUNB0562290) is the one to use now. There's still that October "
             "invoice sitting unpaid, and obviously the ongoing monthly stuff too."),

            ("A5", "inference via third-party framing",
             "Our CFO asked me to write. The facility referenced in your vendor file "
             "was closed at the end of last quarter following an internal "
             "reorganisation. Settlements - both the unpaid October billing and "
             "everything subsequent - are now received at 446168753919, PUNB0562290."),

            ("A6", "maximally oblique",
             "Further to our restructuring, our receivables now flow through a single "
             "facility: 446168753919 / PUNB0562290. INV-4471 remains open on our "
             "ledger and the recurring engagement continues through March. Both are "
             "handled under the new arrangement. The prior arrangement is wound down."),
        ],
    },

    "B_ADD": {
        "expected": ("BENEFICIARY_CHANGE", "ADD_FUND_ACCOUNT", "FUTURE_ONLY"),
        "cases": [
            ("B1", "inference via retained role",
             "We've set up a separate facility for the new business unit: "
             "446168753919, PUNB0562290. INV-4471 from October should still settle "
             "where it always has. Anything raised under the new unit from April "
             "onward comes to the new one."),

            ("B2", "inference via division split",
             "Our infrastructure division now banks separately - 446168753919 "
             "(PUNB0562290). The existing arrangement continues to serve the "
             "consulting engagement including INV-4471. Only infra work billed after "
             "this month routes to the new facility."),

            ("B3", "conversational retained",
             "Heads up - we've opened something new for the retainer side: "
             "446168753919, PUNB0562290. That October invoice is fine where it is, "
             "don't touch that. Just the retainer stuff from next cycle onward."),

            ("B4", "inference via parallel operation",
             "Both facilities now run in parallel on our end. 446168753919 "
             "(PUNB0562290) handles anything invoiced from the coming quarter. What's "
             "already been raised, including INV-4471, settles through the existing "
             "route as before."),
        ],
    },

    "C_CONTROL": {
        "expected": ("PAYMENT_FOLLOWUP", "NONE", "NONE"),
        "cases": [
            ("C1", "control - account number present",
             "Chasing INV-4471. Our records show 446168753919 (PUNB0562290) as the "
             "receiving account, same as it's always been, and nothing has landed. "
             "Could you confirm whether the transfer was released? Our bank has "
             "updated their statement system recently so I want to rule out a "
             "display issue on our end."),

            ("C2", "control - change vocab about process",
             "We've changed our internal invoicing process - new reference format "
             "starting next month. Payment details stay exactly as you have them, "
             "446168753919 / PUNB0562290. Just flagging so the new reference numbers "
             "don't confuse your AP team. INV-4471 still pending by the way."),

            ("C3", "control - someone else's change",
             "FYI our subcontractor switched banks mid-project and it delayed their "
             "invoicing to us, which is why our INV-4471 went out late. Nothing "
             "changes for you - 446168753919, PUNB0562290 as always. Just explaining "
             "the timing."),

            ("C4", "control - historical change reference",
             "Following up on INV-4471. Note we did move to 446168753919 "
             "(PUNB0562290) back in March and you updated it then - I'm not asking "
             "for anything to change now, just confirming that's still what's on "
             "file before I escalate the non-payment internally."),
        ],
    },
}


# ══════════════════════════════════════════════════════════════════════
# KEYWORD / REGEX BASELINE
# A fair first attempt an engineer would actually write.
# Synonym lists, not a single keyword. Not a strawman.
# ══════════════════════════════════════════════════════════════════════

CHANGE_TRIGGERS = ["update", "change", "changed", "new account", "new bank",
                   "replace", "revised", "switch", "switched", "moved bank",
                   "modify", "amend"]
ADD_TRIGGERS    = ["add", "additional", "second account", "supplementary",
                   "alongside", "also add", "another account"]
REPLACE_TRIGGERS = ["replace", "instead of", "no longer", "retired", "supersede",
                    "don't use the old", "do not use the old", "stop using"]
SCOPE_FUTURE_ONLY = ["future invoices only", "from next month",
                     "going forward only", "new invoices", "raised from"]
SCOPE_BOTH = ["outstanding", "pending", "and future", "subsequent",
              "still unsettled", "hereafter", "presently outstanding"]

ACCOUNT_RE = re.compile(r"\b\d{9,18}\b")
IFSC_RE    = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")


def keyword_baseline(text):
    low = text.lower()
    has_acct    = bool(ACCOUNT_RE.search(text))
    has_ifsc    = bool(IFSC_RE.search(text))
    has_change  = any(t in low for t in CHANGE_TRIGGERS)
    has_add     = any(t in low for t in ADD_TRIGGERS)
    has_replace = any(t in low for t in REPLACE_TRIGGERS)

    if has_acct or has_ifsc:
        intent = "BENEFICIARY_CHANGE"
    else:
        intent = "PAYMENT_FOLLOWUP"

    if intent == "PAYMENT_FOLLOWUP":
        action = "NONE"
    elif has_add and not has_replace:
        action = "ADD_FUND_ACCOUNT"
    elif has_replace or has_change:
        action = "REPLACE_PAYOUT_DESTINATION"
    else:
        action = "UNKNOWN"

    if intent == "PAYMENT_FOLLOWUP":
        scope = "NONE"
    elif any(t in low for t in SCOPE_FUTURE_ONLY):
        scope = "FUTURE_ONLY"
    elif any(t in low for t in SCOPE_BOTH):
        scope = "OUTSTANDING_AND_FUTURE"
    else:
        scope = "UNKNOWN"

    return intent, action, scope


# ══════════════════════════════════════════════════════════════════════
# LLM SEMANTIC LAYER
# ══════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a semantic normalization layer for a payment-fraud control system.

Read the message and determine what the sender is actually asking for regarding payout destinations.

Return ONLY a valid JSON object. No markdown fences. No preamble. No explanation outside the JSON.

{
  "intent": "BENEFICIARY_CHANGE" or "PAYMENT_FOLLOWUP",
  "action": "REPLACE_PAYOUT_DESTINATION" or "ADD_FUND_ACCOUNT" or "NONE",
  "scope": "OUTSTANDING_AND_FUTURE" or "FUTURE_ONLY" or "NONE",
  "reasoning": "one short sentence"
}

Definitions:

intent:
- BENEFICIARY_CHANGE = the sender wants THEIR payouts to go to a bank account that is not the one currently on file for them.
- PAYMENT_FOLLOWUP = no change to where THIS sender's money goes is being requested.

action:
- REPLACE_PAYOUT_DESTINATION = the existing account will no longer receive anything from this sender.
- ADD_FUND_ACCOUNT = the existing account keeps some continuing role (e.g. old invoices still settle there, or one division still uses it).
- NONE = no change requested.

scope:
- OUTSTANDING_AND_FUTURE = the new account receives BOTH already-raised unpaid invoices AND future ones.
- FUTURE_ONLY = only invoices raised going forward.
- NONE = no change requested.

CRITICAL: judge by meaning, not by keywords. A message may contain account numbers, IFSC codes, and words like "changed", "moved", "updated" while requesting NO change to this sender's payout destination. Examples: describing a third party's bank change, describing an internal process change, or referring to a change that already happened in the past."""


def http_post(url, headers, payload, timeout=30, max_retries=4):
    """
    Returns (parsed_json, error_string). One is always None.
    Retries automatically on HTTP 429 (rate limit), honouring the
    wait time Groq suggests in its error message.
    """
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload),
                              timeout=timeout)
        except requests.Timeout:
            return None, "request timed out"
        except Exception as e:
            return None, f"network error: {e}"

        try:
            body = r.json()
        except Exception:
            return None, f"HTTP {r.status_code}, non-JSON response: {r.text[:200]}"

        if r.status_code == 200:
            return body, None

        msg = body.get("error", {}).get("message", str(body)[:300])

        # Rate limited — wait and retry rather than failing the case
        if r.status_code == 429 and attempt < max_retries:
            wait = 8.0
            m = re.search(r"try again in ([\d.]+)s", msg)
            if m:
                wait = float(m.group(1)) + 2.0
            print(f"         (rate limited, waiting {wait:.1f}s and retrying)")
            time.sleep(wait)
            continue

        return None, f"HTTP {r.status_code}: {msg}"

    return None, "exhausted retries on rate limit"


def detect_model(api_key):
    """Query Groq for available models, pick the best one we support."""
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
            return preferred, None

    # Nothing from our preference list — fall back to any chat-looking model
    for m in sorted(available):
        if "whisper" not in m and "guard" not in m and "tts" not in m:
            return m, None

    return None, f"no usable chat model found. Available: {sorted(available)}"


def llm_classify(text, model, api_key):
    """Returns (intent, action, scope, reasoning, error)."""
    body, err = http_post(
        CHAT_URL,
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {api_key}"},
        {"model": model,
         "max_tokens": 2000,
         "temperature": 0,
         "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": text}]},
    )
    if err:
        return None, None, None, None, err

    try:
        msg_obj = body["choices"][0]["message"]
        raw = (msg_obj.get("content") or "").strip()
    except (KeyError, IndexError):
        return None, None, None, None, f"unexpected response shape: {str(body)[:300]}"

    # Reasoning models (gpt-oss, qwen3) sometimes put the answer in a
    # separate reasoning field, or return empty content if the token
    # budget was consumed while thinking.
    if not raw:
        raw = (msg_obj.get("reasoning") or "").strip()

    if not raw:
        finish = body["choices"][0].get("finish_reason", "?")
        return None, None, None, None, (
            f"model returned empty content (finish_reason={finish}). "
            f"If finish_reason=length, raise max_tokens.")

    # Strip markdown fences if present despite instructions
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # Some models emit reasoning before the JSON — grab the outermost object
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, None, None, None, f"JSON parse failed ({e}): {raw[:200]}"

    return (parsed.get("intent"), parsed.get("action"),
            parsed.get("scope"), parsed.get("reasoning"), None)


# ══════════════════════════════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════════════════════════════

def run(label, classify_fn):
    print("=" * 76)
    print(label)
    print("=" * 76)

    total = correct = control_fp = errors = 0
    per_group = {}

    for gname, group in CORPUS.items():
        exp = group["expected"]
        gc = 0
        gerr = 0
        print(f"\n--- {gname}   expected: {exp[0]} / {exp[1]} / {exp[2]}")

        for cid, desc, text in group["cases"]:
            got = classify_fn(text)
            if isinstance(got, tuple) and len(got) == 5 and got[4]:
                print(f"  [ERR ] {cid}  {desc}")
                print(f"         {got[4]}")
                total += 1
                errors += 1
                gerr += 1
                continue
            if isinstance(got, tuple) and len(got) == 5:
                intent, action, scope, reasoning, _ = got
            else:
                intent, action, scope = got
                reasoning = None

            ok = (intent, action, scope) == exp
            total += 1
            if ok:
                correct += 1
                gc += 1
            if gname == "C_CONTROL" and intent == "BENEFICIARY_CHANGE":
                control_fp += 1

            print(f"  [{'PASS' if ok else 'FAIL'}] {cid}  {desc}")
            if not ok:
                print(f"         got: {intent} / {action} / {scope}")
                if reasoning:
                    print(f"         why: {reasoning}")

        per_group[gname] = (gc, len(group["cases"]), gerr)

    print()
    print("-" * 76)
    for g, (c, t, e) in per_group.items():
        suffix = f"   ({e} API error{'s' if e != 1 else ''})" if e else ""
        print(f"  {g:12s} {c}/{t}{suffix}")
    pct = 100 * correct / total if total else 0
    print(f"  {'OVERALL':12s} {correct}/{total} = {pct:.1f}%")
    if errors:
        scored = total - errors
        spct = 100 * correct / scored if scored else 0
        print(f"  {'EXCL ERRORS':12s} {correct}/{scored} = {spct:.1f}%   "
              f"({errors} case{'s' if errors != 1 else ''} never reached the model)")
    print(f"  {'CONTROL FP':12s} {control_fp}/4  legitimate follow-ups misread as a change")
    print("-" * 76)
    print()
    return correct, total, control_fp, errors


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print()
    print("BaseDrift — Semantic Ablation Test")
    print()

    # 1. Baseline always runs, no API needed
    b_correct, b_total, b_fp, b_err = run("KEYWORD / REGEX BASELINE", keyword_baseline)

    # 2. LLM run
    if not API_KEY:
        print("!" * 76)
        print("API_KEY is not set — skipping the LLM half.")
        print()
        print("  PowerShell:  $env:API_KEY=\"gsk_...\"")
        print("  cmd:         set API_KEY=gsk_...")
        print()
        print("Then run this script again in the SAME window.")
        print("!" * 76)
        return

    print(f"Key detected: {API_KEY[:8]}...{API_KEY[-4:]}")
    print("Detecting an available model...")

    model, err = detect_model(API_KEY)
    if err:
        print()
        print("!" * 76)
        print(f"Could not reach Groq: {err}")
        print()
        print("Most likely causes:")
        print("  - key is wrong or was revoked  -> make a new one at console.groq.com")
        print("  - no internet / proxy blocking")
        print("!" * 76)
        return

    print(f"Using model: {model}")
    print(f"Pacing at {CALL_GAP}s/call.")
    print("14 cases will take about 2 minutes. Let it run.")
    print()

    def classify(text):
        result = llm_classify(text, model, API_KEY)
        # Groq free tier: 8000 tokens/min, each call ~900 tokens.
        # ~9 calls/min max, so pace at 7s to stay comfortably under.
        time.sleep(CALL_GAP)
        return result

    l_correct, l_total, l_fp, l_err = run(f"LLM SEMANTIC LAYER  ({model})", classify)

    # 3. Comparison
    print("=" * 76)
    print("ABLATION RESULT")
    print("=" * 76)
    bp = 100 * b_correct / b_total if b_total else 0
    lp = 100 * l_correct / l_total if l_total else 0
    print(f"  keyword baseline : {b_correct:2d}/{b_total}  = {bp:5.1f}%    control FP: {b_fp}/4")
    print(f"  semantic layer   : {l_correct:2d}/{l_total}  = {lp:5.1f}%    control FP: {l_fp}/4")
    print(f"  delta            : {lp - bp:+.1f} percentage points")
    print("=" * 76)
    print()
    print("Ground truth requires inference, not keywords:")
    print("  - scope inferred from WHICH invoices are referenced")
    print("  - add vs replace inferred from whether the old account keeps a role")
    print("  - controls contain account numbers and change vocabulary but")
    print("    request no change to this vendor's destination")
    print()


if __name__ == "__main__":
    main()
