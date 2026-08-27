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
                            `python src/pipeline.py` runs the hero demo.
                            Refuses to run without GROQ_API_KEY, by design.
    eval/ablation.py        semantics vs keyword ablation on a SEMANTICS-ONLY
                            prompt: 14/14 vs 0/14 on gpt-oss-120b, 2026-08-27.
                            Not the extractor's prompt. Score is model-dependent.
    data/generate_data.py   seeded generator (seed 42) — vendor master + 800
                            labeled cases, stratified 70/30. Writes beside
                            itself. cases_holdout.csv is gitignored.

## Hard rules — do not break these
- The LLM never decides. It produces evidence; decision_engine decides.
  DESIGN INTENT, not yet true in code — see P0.1 below. Do not cite this as
  a guarantee in the README, the pitch, or a docstring until R2 is fixed.
- Identity is never taken from the request. Always vendor master.
- Callback always goes to vendor.known_phone, never a number in the email.
  (This one IS enforced — verifier.py reads vendor.known_phone directly.)
- Inconclusive -> STEP_UP, never ALLOW. "couldn't check" != "clean".
- "Couldn't evaluate" is not "caught". An R1 hold is correct policy but is
  never scored as a detection — run_case() sets correct=None, scored=False.
- The holdout is scored once, at the end, and reported with its size.

## Status
Built: llm_client, extractor, decision_engine, verifier, pipeline, ablation,
data generator + generated dev/holdout splits.

No test suite exists. An earlier version of this file claimed 38 unit tests
(11 extractor, 15 decision, 12 verifier/pipeline). That was never true — there
are no test files in the repo or in git history. Writing them is item 4 below.

Verified by running (2026-08-27, model openai/gpt-oss-120b):
  - keyword baseline 0/14, 4/4 control false positives
  - LLM semantic layer 14/14, 0/4 control FP  (was 13/14 on the retired model;
    the score moved because the model changed, nothing else)
  - MODEL_PREFERENCE[0] resolves live, no fallback
  - hero case -> R4_bec_pattern -> BLOCK, scored=true, reject + deactivate emitted
  - generator: 120 vendors, 558 dev / 242 holdout, all four types in both splits,
    byte-identical on re-run

Extraction is NOT reproducible run to run even at temperature=0. Six runs of the
identical hero email returned three different hedged_fields spellings. The final
decision held at R4_bec_pattern in all six, but signal-level output varies —
budget for this when scorer.py reports numbers.

## Next — P0 first, these are safety flaws not features

P0.1  decision_engine R2 short-circuits to ALLOW on the LLM's intent label
      alone, before any Tier 1 check runs. One model output releases a payout
      with zero identity validation. Fix: run Tier 1 against the payout's real
      destination even when the semantic layer reports no change; treat "no
      change requested, but destination is unknown" as STEP_UP, never ALLOW.

P0.2  FAVResult.account_status is never read by any signal. An inactive
      account with name_match_score 99 returns PASS, and "unknown" — meaning
      FAV was inconclusive — also passes, violating the inconclusive rule
      above. Fix in check_name_match, and add the row to the rule table.

P0.3  check_account_continuity tests ext.proposed_account_number — the LLM's
      reading of the email — not the payout's actual destination, which
      run_case() never receives. Settle before the webhook handler is built;
      that handler is where the real fund account first becomes available.

P0.4  check_gstin decides PASS-vs-WARN by testing hedged_fields against the exact
      tuple ("gstin", "proposed_gstin"). The model emits at least three spellings
      for the same concept, so a genuine hedge is silently missed when it picks
      another — observed on the hero case, which reported gstin PASS on "should be
      the same as before". Fails OPEN (fewer WARNs -> more ALLOWs), so it is a
      safety bug, not cosmetic. Stop string-matching an open vocabulary.

Then:
1. case -> email renderer. BLOCKS eval/scorer.py: cases_*.csv hold feature
   rows with no message text, and run_case() takes email_text. Must not leak
   the keyword baseline's trigger vocabulary — see the README's v1 note.
2. eval/scorer.py — run the pipeline over data/cases_dev.csv, tune thresholds.
   Extraction is non-deterministic, so a single pass is one sample: report a run
   count and spread, not a bare number, and re-check any threshold across runs.
3. webhook handler for payout.pending
4. unit tests — decision_engine first, it is pure and needs no API key
5. dashboard
6. holdout run (once) + pitch video
