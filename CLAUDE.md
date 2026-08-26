# PayeeProof

Razorpay AI Buildathon — Track 2 (AI Risk Manager).
Read README.md first for the full problem statement and evidence.

## What this is
A pre-authorization decision layer for RazorpayX payouts. It verifies
the *authorization provenance* of a proposed payout destination — the
question FAV, Reverse Penny Drop, and Approval Workflow all leave open.

## Setup
    pip install -r requirements.txt
    $env:GROQ_API_KEY="gsk_..."     # PowerShell
    set GROQ_API_KEY=gsk_...        # cmd

Groq free tier. No credit card. console.groq.com

## Layout
    src/llm_client.py       one place that talks to Groq; model auto-detect,
                            429 retry, reasoning-model handling
    src/extractor.py        THE ONLY LLM STEP. semantic layer + claims.
                            never trusted as identity.
    src/decision_engine.py  deterministic policy. full rule table in the
                            module docstring — code implements exactly that.
    src/verifier.py         callback to vendor_master phone only, never the
                            number in the request. emits RazorpayX API actions.
    src/pipeline.py         run_case() — end to end, returns audit dict.
                            `python pipeline.py` runs the hero demo.
    eval/ablation.py        proves the LLM does semantics not extraction.
                            13/14 vs 0/14 keyword baseline.
    data/                   vendor master + 800 labeled cases, 70/30 split

## Hard rules — do not break these
- The LLM never decides. It produces evidence; decision_engine decides.
- Identity is never taken from the request. Always vendor master.
- Callback always goes to vendor.known_phone, never a number in the email.
- Inconclusive -> STEP_UP, never ALLOW. "couldn't check" != "clean".
- The holdout set is opened exactly once, at the end.

## Status
Done: llm_client, extractor, decision_engine, verifier, pipeline, ablation.
All unit tested (11 extractor, 15 decision, 12 verifier/pipeline).

Next:
1. eval/scorer.py — run pipeline over data/cases_dev.csv, tune thresholds
2. webhook handler for payout.pending
3. dashboard
4. holdout run (once) + pitch video
