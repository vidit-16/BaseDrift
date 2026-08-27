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
    eval/rules_eval.py      scores the DECISION ENGINE only, from case features.
                            No LLM. Always reports against a null baseline
                            because accuracy alone is 100% for doing nothing.
                            --sweep shows R4 threshold sensitivity.
    eval/ablation.py        semantics vs keyword ablation on a SEMANTICS-ONLY
                            prompt: 14/14 vs 0/14 on gpt-oss-120b, 2026-08-27.
                            Not the extractor's prompt. Score is model-dependent.
    data/generate_data.py   seeded generator (seed 42) — vendor master + 800
                            labeled cases, stratified 70/30. Writes beside
                            itself. cases_holdout.csv is gitignored.

## Hard rules — do not break these
- The LLM never decides. It produces evidence; decision_engine decides.
  ENFORCED as of the P0 pass: an ALLOW requires the payout's real destination
  to match the vendor master, on every rule including R2. The worst a hostile
  extraction achieves is downgrading a BLOCK to a hold, never a release.
  tests/test_decision_engine.py holds this property down — keep it that way.
- Identity is never taken from the request. Always vendor master.
- The account checked is the payout's real destination, never the number the
  model read out of the message. The claim is a labelled fallback for offline
  analysis and the audit record always says which was used.
- Callback always goes to vendor.known_phone, never a number in the email.
  (This one IS enforced — verifier.py reads vendor.known_phone directly.)
- Inconclusive -> STEP_UP, never ALLOW. "couldn't check" != "clean".
- Signal has FOUR states. INCONCLUSIVE ("could not check") is not WARN
  ("checked, looks wrong"). Both hold a payout; only WARN may contribute to
  a BLOCK. Never let missing data push a case toward rejection.
- Never report accuracy on cases_*.csv without the null baseline beside it.
  A do-nothing pipeline scores 100%/100%; the real metric is step-up rate.
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

## Done — P0 safety pass

P0.1  R2 no longer short-circuits. It verifies "nothing is changing" against the
      resolved destination: known -> R2a ALLOW, unknown -> R2b STEP_UP, another
      vendor's account -> R2c BLOCK. Old engine returned ALLOW on the label alone
      even with FAV inactive, name score 3, attacker domain and urgency present.
P0.2  check_account_status is a Tier 1 signal. active PASS / inactive FAIL /
      unknown WARN. Old engine returned R7_all_clear on an inactive account.
P0.3  decide() and run_case() take destination_account_number, resolved from the
      payout's fund account. resolve_destination() prefers it over the model's
      claim and records provenance in Decision.destination_source.
P0.4  _hedged() matches the concept inside normalised field names. "gst_number"
      and "gst" were both missed before and failed open.
P0.6  R4 now requires evidence of deliberate impersonation, not just
      contextual risk. It fired on REPLACE + new account + any 2 Tier-2 warns;
      both are true of a legitimate bank change, and it rejected 15.8% of
      legitimate traffic (49/60 rebrands). No threshold fixed it. Now needs a
      deception signal (Signal.deception, currently typosquat domains by edit
      distance) plus >=1 contextual warn. False blocks 15.8% -> 0.6%.
      KNOWN LIMIT: sim-swap fraud from an affiliation-claiming domain
      (vendor-billing.com) is indistinguishable from a rebrand on evidence.
      17 released on dev. The fix is a second verification channel, NOT more
      domain heuristics — the tested alternative put false blocks back to 15.8%.
P0.5  Signal gained INCONCLUSIVE. WARN meant both "looks wrong" and "could not
      check"; only the former should reach a BLOCK. Reclassifying continuity's
      unresolved case briefly reopened the R2 bypass — the regression test from
      P0.1 caught it. R2 now holds on anything that is not a clean PASS.

All six have regression tests in tests/test_decision_engine.py (29 tests, no API
key). Verified they fail against the pre-P0 engine — do not assume a green run
means anything until you have checked a test can fail.

Then:
1. DONE — generator variance. 10 narratives, 90 distinct feature patterns
   (was 4), randomised within each scenario. Adds compromised-mailbox, patient,
   mule-account and sim-swap fraud; rebrand, multi-account-add and unreachable
   legitimate cases; varying FAV availability and account_status.
2. second verification channel for the sim-swap gap (P0.6). The callback is a
   single point of failure and the rules provably cannot cover it.
3. case -> email renderer. BLOCKS the extraction eval: cases_*.csv hold feature
   rows with no message text, and run_case() takes email_text. Must not leak
   the keyword baseline's trigger vocabulary — see the README's v1 note.
4. extraction eval — email text through the real extractor, compared against
   the generator's features. Measures the EXTRACTOR; eval/rules_eval.py already
   measures the rules. Keep them separate: rules tuning must stay instant and
   deterministic. Cache extractions to disk so re-scoring costs nothing, and
   remember extraction is non-deterministic — report a run count and spread.
5. webhook handler for payout.pending
6. unit tests for extractor / verifier / pipeline. decision_engine is done
   (22 regression tests). Extractor tests must not depend on live output —
   it is non-deterministic; stub llm_client and assert on parsing/validation.
7. dashboard
8. holdout run (once) + pitch video
