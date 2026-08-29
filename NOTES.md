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
    COMPLIANCE.md           regime, what is satisfied, what is not. Read it
                            before widening what the model sees.
    src/llm_client.py       one place that talks to Groq; model auto-detect,
                            429 retry, reasoning-model handling
    src/extractor.py        THE ONLY LLM STEP. semantic layer + claims.
                            never trusted as identity. sanitize() filters by
                            Unicode CATEGORY (Cf/Cc + the tag block), not by an
                            enumerated character list — enumeration missed bidi
                            overrides and the tag block entirely. Reports
                            hidden_chars_removed and document_truncated rather
                            than discarding them.
    src/decision_engine.py  deterministic policy. full rule table in the
                            module docstring — code implements exactly that.
    src/verifier.py         callback to vendor_master phone only, never the
                            number in the request. emits RazorpayX API actions.
    src/webhook.py          payout.pending handler. HMAC-SHA256 over the RAW
                            body, constant-time compare, 15-min replay window,
                            event-id dedupe. Resolves the destination from the
                            payout's fund account (P0.3 at the boundary).
                            Correlates the change-request document; "no document"
                            is evidence, not an error — see R2.
                            The handler NEVER decides; it calls decide().
    src/webhook_app.py      ASGI entry.  uvicorn webhook_app:app --app-dir src
                            Store starts EMPTY on purpose — no fund accounts
                            resolve, so everything holds. PAYEEPROOF_SEED_DEMO=1
                            loads fixtures; never make that a fallback.
    src/dashboard.py        operator view. Shows what the webhook response
                            withholds, ON PURPOSE — different audience. Needs
                            auth in front of it in any real deployment.
    src/webhook_demo.py     five signed scenarios over real HTTP.
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
- NOTHING here calls Razorpay. razorpay_actions() emits action PLANS. Do not
  describe the system as calling approve/reject, and do not auto-execute any
  action carrying requires_human_confirmation.
- Channel 2 (penny drop from the old account) OUTRANKS channel 1 (callback).
  Never make them either/or: sim-swap fraud passes the callback.
- Every extraction records model_used and prompt_hash. MODEL_PREFERENCE
  auto-detects, so without them an audit cannot say which model read the
  document. Do not remove them — see COMPLIANCE.md.
- The webhook's safe state is INACTION. A pending payout stays pending unless
  something explicitly approves it, so every error path must simply not
  approve. Never add a "default to allow" branch, for any reason.
- Signature verification uses the RAW request bytes and hmac.compare_digest.
  Do not re-serialise the parsed body to verify it, and do not swap in ==.
- sanitize() filters by Unicode category. Do NOT replace it with a character
  list: the list it replaced missed U+202A-202E and the whole U+E0000 tag
  block, which are the two most effective ways to hide text in a document.
- "Couldn't evaluate" is not "caught". An R1 hold is correct policy but is
  never scored as a detection — run_case() sets correct=None, scored=False.
- The holdout is scored once, at the end, and reported with its size.

## Status
Built: llm_client, extractor, decision_engine, verifier, pipeline, ablation,
data generator + generated dev/holdout splits.

150 tests across 6 suites, none needing an API key: `python tests/run_all.py`
(decision_engine 32, eval_harness 9, extractor 30, render 17,
verifier+pipeline 25, webhook 37).

An early version of this file claimed 38 unit tests when zero existed. The
claim was removed at the time rather than quietly left in place; the suites
that exist now were written afterwards.

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
P0.7  Post-audit fixes (external review, 2026-08-27). All confirmed by
      reproduction before being fixed:
      - extract() raised AttributeError on a JSON array, contradicting its own
        "never raises" guarantee. Top-level type is checked first, every list
        element is type-checked, and conversion is wrapped.
      - decision_engine crashed joining non-string phrase elements. A rule
        engine crashable by its own input is a DoS on the payout queue.
      - resolve_destination() treated a BLANK destination_account_number as
        "not supplied" and fell back to the account the request itself named,
        producing an outright ALLOW. Blank now holds; only None falls back,
        and only for offline document analysis.
      - the webhook's 15-minute freshness window was shorter than Razorpay's
        24-hour retry period, so one transient failure meant the payout was
        never decided. Idempotency is the real replay control; the window is
        now a coarse backstop outside the retry period.
      - the HTTP response returned the full audit record, and then still leaked
        account numbers through the rule's reason string. Nothing identifying
        goes back over HTTP now.
      - x-razorpay-event-id is used for dedupe; the body id is a fallback.
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
2. DONE — second verification channel. Reverse Penny Drop from the account
   ALREADY ON FILE. Channel 2 is AUTHORITATIVE, channel 1 (callback)
   corroborates — "either passes" is wrong because sim-swap has the callback
   PASSING. sim_swap releases 17 -> 0, legit_unreachable holds 30 -> 0,
   recall 92.9% -> 100%, false BLOCK unchanged at 0.6%, false hold 9.5% ->
   11.7%. controls_existing_account=None means channel 2 was not attempted
   (RPD is enabled on request) and channel 1 decides alone, as before.
3. DONE — data/render.py. Deterministic per-case seeds, renderer version
   + sha256 per message, and a leakage guard importing BANNED_VOCABULARY from
   the baseline's own trigger lists. Baseline scores 0/556 on the output.
4. DONE — eval/extraction_eval.py. 60-case stratified sample, 1 run,
   gpt-oss-120b: intent 100%, scope 100%, action 96.6%, claims 94.9-100%,
   channel-manipulation recall only 70.6%. End to end real == ideal, 98.3%
   same rule. Disk cache keyed by email sha + model + prompt hash +
   NORMALIZER_VERSION — bump the last one whenever claim normalisation
   changes, or stale entries outlive the fix.
   Keep the two evals SEPARATE: rules tuning must stay instant and
   deterministic, and this one is neither.
5. DONE — webhook handler for payout.pending. 26 tests, no API key.
6. DONE — unit tests for extractor / verifier / pipeline. 137 tests total
   across 5 suites (decision_engine 32, extractor 32, render 17,
   verifier+pipeline 19, webhook 37). `python tests/run_all.py`. None need an API key: the extractor
   suite stubs llm_client, since live output is non-deterministic and any test
   asserting on it would be flaky by construction.
7. DONE — dashboard. GET / and /case/{payout_id}, mounted on the webhook app.
8. holdout run (once) + pitch video

═══════════════════════════════════════════════════════════════════════
V2 SCOPE — decided with evidence, NOT to be built before v1 ships
═══════════════════════════════════════════════════════════════════════

The v1 holdout is spent. Everything below changes rules, so it needs a FRESH
holdout from a new seed — and that holdout should also carry the inbox threat
scenarios, otherwise it resamples the same distribution and tests nothing new.

V2.0  THE VENDOR MASTER IS ONE-ACCOUNT-PER-VENDOR, AND THAT IS WRONG.
      Promote this above the rest: it is the root of trust for every check in
      the system, and it currently has a reproducible false-BLOCK path.

      What exists today:
        VendorRecord.additional_accounts   exists, all_known_accounts() uses it
        load_vendors() populates it        NO — there is no CSV column
        generator emits second accounts    NO — zero mentions
        test or eval coverage              ZERO
        accounts per vendor in the master  1, for all 120

      Same shape as account_status before P0.2: coded, never populated, never
      exercised. It looks like a feature and is dead code.

      THREE CONSEQUENCES, worst first.

      (a) A CORPORATE GROUP SHARING AN ACCOUNT IS BLOCKED. Reproduced:
          build_account_index() does `idx[acct] = v.vendor_id` in a loop, so
          the second vendor SILENTLY OVERWRITES the first. A payout to that
          shared account, for whichever vendor lost the race, then fires
          R2c_followup_destination_conflict -> BLOCK.

          decision_engine's own comment says sharing an account across contacts
          is "legitimate for corporate groups". The code rejects it. This is the
          false-positive path recorded under V2.1 as "untested, not absent" —
          it is present and reproducible.

      (b) EVERY PAYOUT TO A LEGITIMATE SECOND ACCOUNT HOLDS, FOREVER. A vendor
          with a collections account and a refunds account, or separate
          divisions, gets account_continuity WARN on every payout to the second
          one, because the master records only one. A permanent false-hold
          generator in production.

      (c) legit_add_account IS INCOHERENT. The narrative has the vendor opening
          a second facility while the existing one keeps serving another
          division — but the master never gains that account. The REQUEST is
          modelled and the RESULTING STATE is not, so a later payout to the
          added account stays "new" indefinitely. It also means ADD versus
          REPLACE is untestable end to end, which is the distinction R4's design
          leans on.

      THE FIX.
        - A separate table, not a wider column. Accounts have their own
          attributes: vendor_accounts.csv with
          (vendor_id, account_number, ifsc, status, added_on, verified_by).
        - Many-to-many index: account -> set(vendor_ids), never a dict that
          overwrites. Cross-contact reuse then means "belongs to a vendor that
          is not this one AND not in the same declared group".
        - An explicit group_id on the vendor. A shared account inside a declared
          group is legitimate; the same account across unrelated vendors is the
          mule pattern. Without the group concept those two are
          indistinguishable, which is precisely why it blocks today.
        - Generator: vendors with 1, 2 and 3 accounts; some groups genuinely
          sharing; and legit_add_account updating the master so a follow-up
          payout to the added account passes.

      AND THE PART THAT MATTERS MOST: verified_by is not a nice-to-have column.
      The vendor master is the root of trust for every check here. An account
      that got in without verification is worthless as a trust anchor — the
      destination would be checked against a record an attacker could have
      written. README already names this recursively ("the vendor master's own
      update path needs equivalent protection"), and multi-account makes it
      concrete, because accounts now get ADDED routinely rather than set once at
      onboarding. Every account needs provenance: verified at onboarding, by a
      penny drop, by a callback — and when.

      This is arguably a bigger item than triage. It is the difference between
      "we check against a trusted record" and "we check against a record".

V2.6  WHICH ACCOUNT DOES THE PENNY DROP COME FROM?
      Falls out of V2.0 and is a REDESIGN of verifier.py, not an edit. It was
      invisible while every vendor had exactly one account.

      THE PREMISE CHANNEL 2 RESTS ON: the requester still controls where money
      has been going, and an attacker cannot, because moving money AWAY from
      that account is the entire point of the attack.

      With one account on file, "the account we have been paying" is
      unambiguous. With several it is not, and the premise breaks silently.

      THE ATTACK. An attacker gets account B added to the vendor master — a
      compromised mailbox, a plausible "we have opened a second facility"
      request, which is exactly the legit_add_account narrative. Later they
      request a change to account C. The system asks for proof of control over
      an account on file; the attacker penny-drops from B, which they control;
      channel 2 passes. The strongest control in the system confirms the fraud.

      What happened there: the attacker used a PREVIOUS SUCCESS as the
      credential for the next one.

      THE FIX, in one sentence: the system NAMES the account, the requester
      never chooses. "Send Rs 1 from any account on file" lets the attacker
      pick the one they control. "Send Rs 1 from 434392416664" does not.

      WHICH ACCOUNT QUALIFIES — one the requester could not have planted:
        - has received AT LEAST ONE SETTLED PAYOUT. Money actually arrived, so
          the vendor controlled it at that moment. Being listed proves nothing.
        - added at least SEASONING_DAYS ago. An account added last week is
          exactly what a planted one looks like.
        - NOT added by the channel now being verified. An account added by an
          email request cannot verify another email request; that is circular.
      Choose the OLDEST qualifying account, not the newest.

      WHEN NOTHING QUALIFIES — the part that needs care. Today:
        controls_existing_account=None   -> channel 1 decides alone
                                 =False  -> held
      The instinct is to return None ("not attempted"). THAT IS WRONG: it falls
      back to the callback, which sim-swap defeats, so a vendor with no seasoned
      account and a compromised phone would be RELEASED. There must be a third
      state — channel 2 UNAVAILABLE — which escalates to a human and never falls
      back. Same "inconclusive is not clean" rule as everywhere else.

      It happens legitimately for a genuinely new vendor with no payout history,
      and holding is correct there: nothing exists to compare against.

      THE HONEST LIMIT. A patient attacker plants an account, waits out the
      seasoning window, lets it receive one real payout, then uses it. That
      defeats this. But it requires that they ALREADY SUCCEEDED ONCE, so this is
      containment rather than prevention: it stops one compromise bootstrapping
      the next, which is the realistic threat. Do not claim the channel is
      airtight.

      DATA CONSEQUENCE, bigger than V2.0 currently describes: the vendor master
      alone cannot answer "has this account received a settled payout". This
      needs SETTLEMENT HISTORY, not just an account list.

V2.1  STOP REJECTING AUTOMATICALLY.  Measured on dev, 556 cases:

        policy                              recall  falseBLK  rejects  holds
        reject on any Tier 1 FAIL (v1)      100.0%      0.6%      149    130
        reject only on cross-contact reuse  100.0%      0.0%       34    244
        never reject                        100.0%      0.0%        0    278

      Removing BLOCK costs NOTHING in capture. Not one fraud case is released,
      because BLOCK never stops fraud a HOLD would not also stop — the money
      does not move either way. BLOCK buys operational convenience only, and
      pays for it with the single customer-facing failure the system has.

      Restricting rejection to cross-contact reuse also reaches 0.0%, but that
      is partly a dataset artifact: decision_engine already notes that RazorpayX
      permits one account under multiple contacts, which is LEGITIMATE for
      corporate groups. The generator has no shared-account scenario, so that
      false-positive path is untested rather than absent. Add it in v2.

      Recommended: no automatic rejection at all. A human confirms every one.
      This is the same principle already applied to fund-account deactivation
      (requires_human_confirmation); v1 only half-applied it.

      Cost at real volume: a handful of extra holds per day. See the volume
      model — even 20,000 payouts/day yields ~40 change requests.

V2.2  TRIAGE — and most of it is NOT an agent.

      Today POST /documents is hand-fed a vendor_id. A real AP inbox is ~500
      messages/day of invoices, chasers, statements, internal mail and spam,
      and nobody has done that routing.

        INBOX ~500/day
          [RULES]  ingest: dedupe by message-id, drop auto-replies,
                   no-reply senders, extract attachments        -> ~400
          [RULES]  vendor resolution: sender domain vs vendor master
                   OR a lookalike of one OR a known contact name -> ~30
          [1 CALL] relevance + intent: is a destination change asked for? -> ~3
          [AGENT]  investigation, ONLY for the residual few
          [DET]    existing decision engine

      Vendor resolution does ~90% of the filtering and needs NO model. A
      classification call is a classifier, not an agent. Putting an agent in
      the hot path of every inbound email is expensive and non-deterministic
      for no benefit.

      TRAP: filtering on "sender domain is in the vendor master" drops the
      fraud. A typosquat is BY DEFINITION not in the master. Stage B must be
      "in the master OR a lookalike of something in it" — reuse
      decision_engine.is_lookalike_domain(), do not rebuild it.

V2.3  MCP INBOX — a tool layer, not a stage. It appears twice:
        (1) SOURCE   list_messages / get_message — without it there is no
            triage input at all.
        (2) TOOLS    get_thread / search_history / prior_change_requests —
            called by the investigation agent.

      The agent is the only place with a genuine loop: read, discover it needs
      history, fetch, re-evaluate. ~3 emails/day reach it, which is what makes
      an agent affordable there and wasteful upstream.

      HARD CONSTRAINTS, both following from v1's invariants:
      - Inbox-derived signals are TIER 2 ONLY. "No prior history from this
        sender" corroborates; it can never establish identity and can never
        be what releases a payout.
      - Every MCP tool is READ-ONLY and scoped to one merchant's own mailbox.
        The agent reads attacker-controlled content while holding tools, so
        that is a prompt-injection surface by construction. Containment is:
        read-only tools, narrow scope, and output that can only downgrade a
        release to a hold — never the reverse.

      What the inbox unlocks that a single message cannot carry: thread depth
      (first contact vs 40th), thread hijacking (real quoted history, new
      sender), and change-request history (third bank change this quarter).
      It does NOT rescue the compromised-mailbox case — the history there is
      genuine.

V2.X  WHICH BUILDING BLOCK EACH ITEM TOUCHES

      block                       2.1  2.5  2.0  2.6  2.2  2.3
      1  Entry (webhook)           -    -    -    -   rew  rew
      2  Evidence (extractor)      -    -    -    -    -    -     <- UNTOUCHED
      3  Trust store              --   --  RBLD  +hist  -    -
      4  Signals                   -   sml  chg   -    -   ext
      5  Rules                    sml   -    -    -    -    -
      6  Verification              -    -    -   RDSGN -    -
      7  Actions                  sml   -    -    -    -    -
      8  Observability             -    -   min  min   -   min
      9a vendor_master.csv         -    -   schema +hist -   -
      9b cases_dev.csv             -    -   REGEN REGEN fields fields
      9c cases_holdout.csv         -    -   REGEN REGEN fields fields
      9d extraction cache          -    -   VOID  VOID   -    -
      9e evaluators                -    -   chg   chg  new  new
      NEW triage                   -    -    -    -   BUILD  -
      NEW investigation agent      -    -    -    -    -   BUILD

      Block 2, the extractor and the model client, is touched by NOTHING in v2.
      It is the piece people assume is riskiest and it is the most stable thing
      here — 100% on intent and scope across dev AND holdout, with errors that
      provably do not move decisions.

      Row 9d is the schedule risk. ANY generator change invalidates every cached
      extraction: 2.1 h to re-extract 800 cases on a clean run, ~9.6 h
      rate-limited, and roughly 7 DAYS against the free tier's ~200k tokens/day.
      Therefore: ONE generator pass. Multi-account, groups, settlement history,
      new scenarios and the new seed all land together, or that cost is paid
      several times over.

      Rows 9b and 9c move together and are not optional. Dev and holdout come
      out of a single generate_cases(800) call split 70/30, so they cannot be
      changed independently; they must share a schema, because the evaluators
      read both with the same code; and they must share a DISTRIBUTION, because
      dev's whole job is to be a proxy for unseen data. Scenarios present only
      in holdout would make its result uninterpretable — a genuine
      generalisation gap would be indistinguishable from a distribution mismatch
      we created ourselves.

V2.4  FRESH HOLDOUT. New seed, and add the scenarios v1 lacks: corporate
      groups legitimately sharing an account (see V2.0(a) — this one is
      reproducible today), vendors with two or three legitimate accounts,
      a payout to an account added by an earlier accepted request, first-contact
      fraud, and thread hijacking. Without new scenarios a new seed only
      resamples the same distribution.

      AND SAY THIS PLAINLY RATHER THAN CLAIMING A VIRGIN TEST SET: v1's holdout
      has been seen, and what it showed shaped v2's design — the inactive fix
      and the no-reject decision both came from reading it. That cannot be
      un-seen. What a new seed plus new scenarios buys is that the parts of v2
      which matter most — multi-account, corporate groups, the verification
      account, triage — are tested on ground nothing has ever been tuned
      against. That is a smaller claim than "fresh holdout" implies and it is
      the true one.

V2.5  Also carry over from the v1 holdout finding: account_status=inactive
      should be INCONCLUSIVE, not FAIL. It caused ALL FIVE false blocks across
      both splits, occurs on 2.0% of cases in both, and is UNCORRELATED with
      fraud (dev 7 fraud/4 legit; holdout 2 fraud/3 legit). It was a P0.2
      overcorrection — from ignoring the field entirely to treating it as a
      hard identity conflict. Deliberately NOT fixed in v1 because the holdout
      was already open and tuning against it would have invalidated the result.

