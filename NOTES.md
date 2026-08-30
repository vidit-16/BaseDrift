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

V2.0  ── DONE (Phases 3-4), not yet committed. See V2.R for what it
      measured. Original scope follows.

      THE VENDOR MASTER IS ONE-ACCOUNT-PER-VENDOR, AND THAT IS WRONG.
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

V2.6  ── DONE (Phase 4), not yet committed. select_verification_account()
      in verifier.py; the third state is UNAVAILABLE_C2. See V2.R.

      WHICH ACCOUNT DOES THE PENNY DROP COME FROM?
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

V2.1  STOP REJECTING AUTOMATICALLY.  ── DONE (Phase 1), not yet committed.

      Shipped: every rule that returned BLOCK now returns STEP_UP carrying
      recommended_action="reject". razorpay_actions() emits the reject and the
      deactivate call, both flagged requires_human_confirmation, so a reviewer
      sees the recommendation and one click acts on it while nothing acts on it
      alone. There is no BLOCK outcome left in the engine; a structural test
      asserts the literal is gone, because a behavioural sweep only covers the
      inputs someone thought of.

      Measured on dev (556 cases), against the v1 baseline:

        metric              v1        v2 phase 1
        recall            100.0%        100.0%     unchanged, no fraud released
        precision          86.0%         86.3%
        false BLOCK         0.6%          0.0%     and now 0 BY CONSTRUCTION
        held               52.7%         79.5%
        false hold         11.7%         12.0%
        recommendations       —      144, 0 legitimate

      Read the last two rows together. The step-up column rose because the 149
      cases v1 rejected are now holds, so "held" no longer means "needs a phone
      call" — 144 of those holds are recommendations awaiting a click. The eval
      prints both columns for exactly that reason: with rejection removed,
      false BLOCK is zero by construction and says nothing on its own.

      THE FINDING THIS TURNED UP, which the plan did not anticipate.
      R2c, R3 and R4 used to END the case. Now they hold — so for the first
      time their cases flow INTO verification, and a passing channel would
      RELEASE a payout the previous version rejected. Recall would have fallen
      silently, and the release would have read in the audit as an ordinary
      verified change.

      So verify() gained one rule: A HOLD CARRYING A REJECTION RECOMMENDATION
      IS NEVER AUTO-RELEASED. The channels still run and their results are
      recorded as evidence, but the case ends with a human. Evidence of
      impersonation, an identity conflict against the master, or a destination
      belonging to a different vendor are not things a phone call or a rupee
      clears.

      HONEST NOTE ON THAT GUARD: the dev split does not exercise it. Recall is
      100% with the guard disabled, because no fraud case in the data both
      recommends rejection and passes a channel. Two tests cover it and both
      fail without it, but they are constructed, not sampled — this is a
      correctness property with no data behind it yet, which is the exact shape
      of defect this project keeps finding. Phase 3 must generate the case:
      fraud, recommended for rejection, controls_existing_account=True. That is
      the V2.6 planted-account attack, and until it exists in the data the guard
      is asserted rather than measured.

V2.1  BASELINE MEASUREMENT that drove the above.  Measured on dev, 556 cases:

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

V2.2  ── DONE (Phase 5). See V2.T. Original scope follows.

      TRIAGE — and most of it is NOT an agent.

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

V2.3  ── DONE (Phase 5). See V2.T. Original scope follows.

      MCP INBOX — a tool layer, not a stage. It appears twice:
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

V2.R  WHAT PHASES 3 AND 4 ACTUALLY PRODUCED  (measured, dev split, 551 cases)

      ── The corpus ──────────────────────────────────────────────────
      seed 20260829, AS_OF 2026-06-30, 120 vendors with UNIQUE domains, 272
      accounts on file, 800 cases split 552 / 248. Accounts per vendor
      {1:34, 2:39, 3:33, 4:10, 5:3, 6:1}. 46 vendors across 20 declared groups,
      14 of those groups genuinely sharing a facility, and NO account shared
      across two different groups. 800 rendered messages, 800 distinct, ZERO
      leakage failures, byte-identical across two full runs.

      THIS CORPUS IS THE SECOND ONE. See V2.B below — the first was generated
      wrong throughout, and its numbers, including a scored holdout, belong to
      a dataset that no longer exists.

      ── The three v1 defects, closed and measured ───────────────────

        legit_group_shared_account   25 of 25 ALLOW   (v1: REJECTED)
        legit_second_account         22 of 22 ALLOW   (v1: held, every time,
                                                       forever)
        legit_added_then_paid        16 of 16 ALLOW   (v1: could not exist)

      ── V2.6, and the number that justifies it ──────────────────────

      The counterfactual, run against the same corpus with the OLD channel-2
      policy — "prove you control an account on file", the requester choosing
      which:

        released under "any account on file"    26 fraud_planted_account cases
        released under the NAMED-account policy 0

      All 26. Every one of them a case where the attacker had earlier got an
      account onto the vendor master and now penny-drops from it, and the
      strongest control in the system confirms the fraud. Recall would have been
      ~90.3% rather than 100%, and every miss would have read in the audit as a
      successfully verified change.

      THE PRICE, stated because it is not zero: 10 legitimate cases moved from
      released to held (3.5% of legitimate traffic). Those are vendors with no
      account that qualifies to prove anything — genuinely new suppliers, no
      settled payout yet — and holding them is correct, because nothing exists
      to compare against. The trade is 26 fraud caught for 10 calls made.

      Channel 2 unavailable fired on 43 cases (7.8%), 30 fraud / 13 legit. It
      escalates and never falls back to the callback, which is the whole point:
      falling back would release exactly the sim-swap case the second channel
      was added for.

      AND THE HONEST CAVEAT ON THE 26/26. That figure is a property of a corpus
      built to contain the attack. The attack itself is real — establishing a
      foothold and using it later is ordinary BEC tradecraft — but 26 of 26 is
      not a field measurement, and the generator and the fix were written by the
      same hand. What it demonstrates is that the OLD policy is defeated by a
      case this corpus can now express and the v1 corpus could not.

      ── The cost side of the ledger, not buried ─────────────────────

        metric        v1 dev   v2 phase 1   v2 final   null baseline
        recall         100.0%      100.0%     100.0%          100.0%
        precision       86.0%       86.3%      85.9%           83.8%
        held            52.7%       79.5%      78.4%          100.0%
        false BLOCK      0.6%        0.0%       0.0%            0.0%
        false hold      11.7%       12.0%      14.9%           17.7%

      FALSE HOLD ROSE, 12.0% -> 14.9%, and the cause is the third state: a
      legitimate vendor with no seasoned account is now held even when the
      callback answers. Against a null baseline of 17.7% that is a thin margin,
      and pretending otherwise would be the same error this project keeps
      finding in itself. What the rules still buy over holding everything is the
      RELEASE rate — 21.6% of payouts need no phone call at all — plus the fact
      that the holds are triaged: 135 of them carry a rejection recommendation
      and none of those 135 is legitimate.

      Note also that the split is not comparable case-for-case with v1: new
      seed, new scenarios, 48.8% fraud versus v1's 43%. The v1 column is
      context, not a controlled comparison.

V2.T  WHAT PHASE 5 PRODUCED — TRIAGE AND THE INBOX  (V2.2, V2.3: DONE)

      New: src/triage.py, src/inbox_signals.py, src/investigator.py,
      mcp/inbox_server.py, data/generate_inbox.py, eval/triage_eval.py,
      tests/test_triage_inbox.py (31 tests). 205 tests in total, all green.

      ── The corpus ──────────────────────────────────────────────────
      data/generate_inbox.py READS the already-rendered cases and surrounds
      them with noise, so it cannot void an extraction. dev: 7,163 messages,
      551 of them genuine change requests (7.7%). The noise is adversarial to
      the CHEAP stages on purpose — 30.6% of it quotes an account number,
      because real invoices do, so a keyword pre-read cannot separate the
      classes on its own.

      ── THE TRAP, AND THEN THE OTHER HALF OF IT ─────────────────────

      "Only read mail from senders in the vendor master" is the obvious first
      stage. Measured on the dev inbox, it discards 170 fraudulent change
      requests — 64.6% OF ALL THE FRAUD IN THE MAILBOX — while improving every
      operational number in sight. A typosquat is by construction not in the
      master; that is what makes it a typosquat.

      That much was anticipated. What was NOT, and what only appeared by
      evaluating: domain matching WITH lookalikes still lost 119 genuine change
      requests — 51 legitimate rebrands (an acquired vendor writing from the
      parent's domain, which resembles nothing) and 68 fraud cases whose forged
      domain was too far from the original to register as a typosquat. Both
      populations are simply "a sender not in the master", and an allowlist
      cannot tell them apart.

      Fix: a third match kind, CONTENT — the body quotes a GSTIN or legal name
      that IS in the master. Recovered all 119, pulled in 0 of 1,474
      unknown-sender noise messages, and took change requests surviving triage
      from 66.1% to 100.0%. Labelled as the weakest of the three matches,
      because anyone can type a GSTIN: a reason to READ, never a claim about
      who sent it.

      A second defect the same eval turned up: the pre-read was vocabulary, and
      "Details on your file are 416961125393 / SBIN0980865" names a destination
      using none of the words. 73 genuine messages dropped for it. Now matched
      structurally, on a long digit string adjacent to an IFSC-shaped token.

      ── Cost ────────────────────────────────────────────────────────
      31.2% of the mailbox resolved with NO model call; 7,163 messages become
      2,367 reaching extraction, a 67.0% reduction. CAVEAT recorded in the eval
      itself: authored noise is easier than real mail — no forwarded chains, no
      attachment-only messages, no vendors who write like spammers — so treat it
      as an upper bound from a corpus built by the same people as the funnel.

      ── WHY MISROUTING HERE IS NOT A MONEY RISK ─────────────────────
      Triage decides what gets READ; it decides nothing about a payout. The
      control point is payout.pending, which fires whether or not any email was
      seen. A wrongly dropped change request becomes a payout with
      evidence_source="no_document_supplied", and R2 then rules on the REAL
      destination — known account passes, unseen account holds, another vendor's
      account still fires R2c. The mule check survives the message never being
      read at all. The failure mode of the whole layer is an unnecessary hold.

      ── V2.3, and the one rule that carries it ──────────────────────
      INBOX EVIDENCE CAN HOLD A PAYOUT AND CAN NEVER RELEASE ONE. Enforced, not
      described: every inbox signal is WARN or INCONCLUSIVE and never PASS, and
      decide() rejects anything else AT THE DOOR.

      That guard was first placed where the signals are used, which put it after
      R1 and R2 — so a follow-up short-circuited to ALLOW without it ever
      running, and whether a smuggled signal was caught depended on which rule
      fired. A guard only some paths reach is not a guard. Moved ahead of the
      rule table.

      The reason for the asymmetry: mailbox history is the most
      attacker-shapeable evidence in the system. Someone inside a mailbox can
      send themselves messages, build a thread to any depth, and manufacture
      months of correspondence. So a long, ordinary, established correspondence
      produces NO SIGNAL — the absence of a warning, never a reassurance.

      MCP tools: get_message, get_thread, search_history, prior_change_requests,
      thread_depth. All read-only, all scoped to one merchant, both properties
      asserted structurally by the tests rather than documented. The agent reads
      attacker-controlled text while holding tools, and the only thing stopping
      an injection from acting is that there is nothing to act with.

      search_history takes `before` so the mailbox is read AS IT WAS when the
      message arrived. Without it the agent reads the future and "has this
      sender written before?" gets answered with mail that had not yet arrived.

      ── WHAT IS NOT EVALUATED, SAID PLAINLY ─────────────────────────
      investigator.py's optional `reasoner` hook — an LLM choosing tool calls —
      is UNEVALUATED. There is no eval corpus for it and no budget to build one
      before the holdout. The deterministic evidence gathering is what runs and
      what the tests measure. An unevaluated agent loop presented as a feature
      is exactly the shape of defect this project keeps finding in itself, so it
      is wired to be evaluable and labelled as not yet evaluated.

      The triage CLASSIFIER stage was also unevaluated. It no longer is — see
      V2.C above, where it is measured and comes out costing about six times
      what it saves while adding nothing to recall.

V2.7  THE PROVIDER IS CONFIGURATION, NOT CODE  (DONE, Phase 6 prep)

      Not in the original v2 scope. It arrived because Groq's free tier caps at
      200,000 tokens/day and re-extracting 800 cases needs ~1.25M, so the run
      was 9-12 days — and Groq's paid tier was not available to sign up for.

      ── The measurement that reframed the problem ───────────────────
      A live probe, rather than the estimate in the plan:

        prompt_tokens       917
        completion_tokens   644   of which 483 are REASONING tokens
        reserved per call  2917   = 917 + max_tokens(2000)

      Groq reserves prompt + max_tokens against the daily cap, so DEFAULT_MAX_
      TOKENS was 69% of the cost of every call. The obvious fix was to lower it.

      IT DOES NOT WORK, and finding out cost one call: max_tokens=700 returns
      HTTP 400. The model spends 483 tokens reasoning before the first JSON
      character, so the response truncates mid-object and fails to parse — which
      arrives downstream as an EXTRACTION FAILURE, not a configuration error.
      That is the same shape as the bug that once cached 201 transport failures
      as real results. A test now pins DEFAULT_MAX_TOKENS >= 1400.

      ── What actually solved it ─────────────────────────────────────
      gpt-oss-120b is OPEN-WEIGHT. Groq does not own it; ~18 companies run the
      same weights. So the fix was to change WHO RUNS THE MODEL, not which
      model — which costs nothing, because the weights are identical:

        provider     input/M   output/M   800-call run
        CoreWeave      $0.03      $0.17      $0.11
        DeepInfra      $0.037     $0.17      $0.13
        Groq           $0.15      $0.60      $0.37   (tier unavailable)

      The alternatives on the table were closed, hosted-only models behind a
      major vendor's key. Any of them forfeits two things: the
      open-weight self-hosting argument that is COMPLIANCE.md's entire answer to
      RBI payment-data localisation, and comparability with v1's extraction
      measurements. Neither was worth saving nothing.

      ── The claim this makes testable ───────────────────────────────
      COMPLIANCE.md said llm_client.py is the only module that talks to a
      provider, so moving inference in-country is a one-file change. Never
      exercised, and as written NOT TRUE: eval/ablation.py is deliberately
      standalone and carried its own hardcoded provider URL. The claim now
      says "the only module in the DECISION PATH", and ablation reads the same
      variables — an evaluation that can silently measure a different provider
      than the system runs on is not evidence about the system. It is now FIVE ENVIRONMENT VARIABLES and it has been
      exercised against a second provider:

        PAYEEPROOF_BASE_URL   provider root (default: Groq)
        PAYEEPROOF_API_KEY    falls back to GROQ_API_KEY, so existing setups
                              keep working and this is additive
        PAYEEPROOF_MODEL      pin an id, skipping detection
        PAYEEPROOF_PROVIDER   OpenRouter: pin WHICH host serves the model
        PAYEEPROOF_CALL_GAP   7.0s is a Groq free-tier figure that costs 93
                              minutes over 800 calls anywhere else

      MODEL_PREFERENCE gained the bare "gpt-oss-120b" spelling: Groq and
      OpenRouter publish the model as openai/gpt-oss-120b, Cerebras drops the
      prefix. Without both, detection on Cerebras would silently fall through to
      a DIFFERENT model — a model swap nobody chose and nothing would report.

      ── WHY served_by IS IN THE AUDIT RECORD ────────────────────────
      OpenRouter routes one model id across ~18 hosts. The first live call
      through it was served by "Mancer 2" — a host that had not been costed and
      that nothing in the record would have named.

      "gpt-oss-120b decided this payout" is therefore not a traceable claim. So
      ExtractionResult gained served_by, llm_client reports the host and the
      token usage back through meta, and PAYEEPROOF_PROVIDER pins routing with
      allow_fallbacks=False — because a pin that silently falls back is worse
      than no pin at all: the audit would name one company while another ran the
      model.

      Nine tests cover the boundary, including that a trailing slash on the base
      URL does not produce a double slash, that a malformed CALL_GAP falls back
      rather than crashing a run that has been going for hours, and that no
      provider field is sent when none is pinned.

V2.B  THE CORPUS WAS GENERATED WRONG, AND EVERY TEST PASSED

      Found while verifying a figure for the README, not by anything in the
      suite: the file said 213 accounts where the documentation said 272.

      THE DEFECT. The domain de-duplication loop, added late to stop 57 of 120
      vendors sharing a domain, used `n` as its collision counter:

          n = 2
          while cand in taken:
              cand = base.replace(".com", f"{n}.com")
              n += 1
          ...
          for g in range(max(1, n // 6)):     # n is the VENDOR COUNT parameter

      `n` is generate_vendor_master's own parameter, 120. Most vendors have no
      collision, so it left the loop as 2, and 2 // 6 is 0. Twenty declared
      groups became ONE.

      WHAT THAT PRODUCED. A master with one group of three vendors sharing one
      account — and all 37 legit_group_shared_account cases across both splits
      drawn from that single configuration. The headline result, "corporate
      groups 12/12 allowed on the holdout", was true and nearly meaningless: it
      exercised one group, one shared account, twelve times.

      AND IT WAS NOT LOCAL. One group instead of twenty is a different number of
      RNG draws, so the stream shifted and every subsequent value changed. Of
      the old and new dev splits, ZERO rows are byte-identical and only 379 case
      ids even overlap. The first corpus was not a narrower version of the
      second; it was a different dataset.

      WHY NOTHING CAUGHT IT. Every one of the 216 tests passed, both evals ran
      clean, the generator was byte-identical across two runs, and the leakage
      guard reported 0/551. One declared group IS a structurally valid vendor
      master. Nothing asserted that the corpus contained ENOUGH of a scenario to
      measure it — which is this project's own recurring finding, a coded
      capability with no data behind it, except applied to the data itself.

      THE GUARD. test_the_corpus_actually_contains_the_scenarios_it_claims now
      puts a DIVERSITY floor on the master: unique domains, >= 10 declared
      groups, >= 5 shared accounts, and every shared account confined to a
      single group. A shape assertion could not have caught this; only a floor
      on coverage can.

      THE HOLDOUT WAS THEREFORE SCORED TWICE, and both runs are reported. This
      is not a discipline violation dressed up: nothing was tuned from what the
      first holdout showed, the regeneration was forced by a figure that did not
      reconcile rather than by a result anyone disliked, and the second corpus
      is a different dataset rather than a second look at the same one. But
      "scored once" is a claim this project makes, so the exception is recorded
      rather than quietly absorbed.

V2.C  TRIAGE STAGE 4, MEASURED — AND IT DOES NOT PAY FOR ITSELF

      The classifier was the last thing in v2 carrying a label instead of a
      number. It now has one, and the number is bad. Recorded here rather than
      quietly kept, because a component that survives only by being unmeasured
      is the exact defect this project keeps finding.

      405 messages, stratified across all seven populations that reach stage 4,
      scored against the deterministic pre-read it replaces.

        approach      prec   recall     F1     FN     FP
        pre-read     26.6%   100.0%   42.0%     0    138
        classifier   32.1%   100.0%   48.5%     0    106

      RECALL IS 100% FOR BOTH. The model catches nothing the free check misses.
      Its entire contribution is precision — 32 fewer messages sent to
      extraction out of 405 — and that is a cost saving, not a capability.

      SO PRICE THE COST SAVING. Projected onto the full dev inbox using the
      per-population rates:

        classifier calls spent    4936
        extractions avoided        395

      4,936 model calls to avoid 395. A classifier call is roughly half an
      extraction call in tokens (~390 prompt against the extractor's 917, with
      a shorter completion), so call it ~2,470 extraction-equivalents spent to
      save 395. A SIX-FOLD NET LOSS, and 12x by call count.

      Stage 4 is not a filter that pays for itself. On this corpus it is a tax.

      WHERE THE ERRORS ACTUALLY ARE
        corpus:ADD/NONE/REPLACE   100% both — every real case routes, both ways
        noise:chaser, logistics   100% both
        noise:invoice             44.6% model / 40.3% pre-read   n=1719
        noise:statement           47.3% model /  0.0% pre-read   n=678

      Two populations carry all of it, and both for the same reason: invoices
      and statements REPRINT STANDING BANK DETAILS, because real ones do. The
      prompt says so explicitly — "an attached invoice that merely reprints
      standing bank details is not a message about the destination" — and the
      model still routes 55% of them. The instruction does not land.

      Statements are the one place the model clearly beats the baseline, 47.3%
      against 0.0%, because the pre-read has no way to distinguish them at all.
      That is a real gain on 678 messages and it does not come close to paying
      for 4,936 calls.

      THE INSIGHT THE NUMBERS POINT AT, not yet built
      -----------------------------------------------
      The discriminator here is not semantic. An invoice or statement quoting
      the account the vendor ALREADY HAS ON FILE is restating, not requesting —
      and stage 3 has already resolved the vendor, so that check is a vendor
      master lookup. Free, deterministic, and aimed exactly at the two
      populations that carry every error.

      NOT IMPLEMENTED, because it needs a question answered first: it would
      also drop the 115 corpus follow-ups, which quote a known account too. That
      may be FINE — webhook.no_document_evidence() already returns
      INTENT_FOLLOWUP / ACTION_NONE, which is precisely what a follow-up
      document asserts, so R2a, R2b and R2c fire identically whether the
      message was read or not. What is lost is Tier 2 evidence from the
      document: sender domain, GSTIN, pressure signals.

      Whether that loss matters is measurable against rules_eval and has not
      been measured. Deciding it from the armchair is how the last three
      defects in this file got written.

      WHAT THIS DOES NOT SAY
      ----------------------
      Not that classification is useless — that statements result is real. Not
      that a cheaper or better-prompted model would fail: neither was tried,
      and both are obvious next steps. And the noise here is authored, so the
      populations are cleaner than an AP inbox.

      What it does say is that the version that shipped, measured on the corpus
      it ships with, costs about six times what it saves and adds nothing to
      recall. Stage 4 stays in the code because it is now evaluable and the
      alternatives are worth trying against it. It should not be described as
      part of the working funnel until one of them beats the free check.

V2.P  THE RAZORPAYX INTEGRATION POINT IS UNTESTABLE IN A SANDBOX — CONFIRMED

      The open question from the v2 plan: does test mode emit payout.pending at
      all? Answered, and the answer closes the option rather than opening it.

      Asked of RazorpayX's own assistant, the reply was that payout.pending is
      supported, fires whenever a payout moves to pending, and applies to all
      payouts. True, and it answers a different question than the one that
      matters. The docs answer this one:

        "The Approval Workflow is not available in the test mode. This means
         the `pending` and `rejected` states are not available in the test
         mode."
        — https://razorpay.com/docs/x/dashboard/test-mode/

      Test-mode payouts start in `processing`, or `queued` on a short balance.
      So a payout can never REACH pending there, and an event that fires on
      entering a state that cannot be entered never fires.

      AND THE OUTBOUND HALF IS BLOCKED TOO, which had not been considered.
      POST /payouts/{id}/approve and /reject operate on pending payouts. With
      no pending state there is nothing for them to act on. The partial
      integration sketched in the v2 plan — replay the event inbound, execute
      approve/reject outbound against test mode — therefore does not work, and
      is withdrawn rather than left standing as a plan.

      WHAT IT ACTUALLY TAKES: live mode, Approval Workflow enabled, a real
      RazorpayX current account, and Payout Approval API access via a
      Technology Partner or OAuth arrangement. Commercial and onboarding
      prerequisites, not engineering ones.

      THE USEFUL CONSEQUENCE. "Nothing here calls Razorpay" reads like a gap
      someone declined to close. The accurate statement is that the control
      point cannot be reached without a live account and real money moving.
      That is a documented platform constraint with a quote behind it, and it
      is a much better sentence.

      The honest next rung is unchanged and is not a code change: shadow mode
      against a willing merchant, deciding nothing and logging what it would
      have done. That is also the only thing that produces a real
      false-positive rate, which is the number that decides deployability.

V2.S  THE SCHEMA, DECIDED IN FULL BEFORE THE GENERATOR RUNS (Phase 2)

      Written down because the generator pass can only be paid for once: it
      voids every cached extraction, and re-extracting 800 cases costs ~2 h on
      a clean run, ~9.6 h rate-limited, ~7 days against the free tier's daily
      token cap. A column remembered afterwards costs that again. So every
      column V2.0, V2.6, V2.2 and V2.3 will need is decided here, including the
      ones nothing reads yet.

      ── data/vendor_accounts.csv (NEW) ──────────────────────────────

        vendor_id             FK to vendor_master
        account_number        the account
        ifsc
        status                active | dormant | closed
        added_on              ISO date
        added_via             onboarding | portal | email_request | phone_request
        verified_by           onboarding_kyc | penny_drop | callback | unverified
        verified_on           ISO date, blank when unverified
        settled_payout_count  int, payouts that SETTLED to this account before
                              the case's own date
        last_settled_on       ISO date, blank when the count is 0
        is_primary            true for exactly one row per vendor

      A separate table rather than a wider vendor_master column, because these
      are attributes OF THE ACCOUNT, not of the vendor. A pipe-joined column
      could hold the numbers and nothing else, and every one of the remaining
      fields is load-bearing:

        added_via    is the circularity check. V2.6 requires that the account
                     naming the penny drop was NOT added by the channel now
                     being verified — an account added by an email request
                     cannot verify another email request.
        verified_by  is the trust-anchor question. The vendor master is the root
                     of trust for every check in this system, so an account that
                     entered it unverified is worthless as an anchor: the
                     destination would be checked against a record an attacker
                     could have written. Not a nice-to-have column.
        settled_*    is "money actually arrived here", which being listed does
                     not prove.

      ── data/vendor_master.csv (CHANGED) ────────────────────────────

        DROP  known_account_number, known_ifsc
        ADD   group_id           blank, or a shared id for a declared group

      known_account_number moves into vendor_accounts as the is_primary row.
      Keeping it in both places would let them diverge, and the divergence would
      be silent — the loader reads one, the eval prints the other.

      VendorRecord.known_account_number STAYS as an attribute; load_vendors()
      derives it from the is_primary row. 23 call sites keep working, the data
      has one source of truth, and the loader asserts exactly one is_primary row
      per vendor rather than picking whichever came first.

      THE TRAP IN group_id, recorded before it is written rather than after:

          if a.group_id == b.group_id:   -> legitimate      # WRONG

      Two blanks are equal. That releases every pair of ungrouped vendors — it
      turns the mule check off for the ~85% of the master with no group. It must
      be:

          if a.group_id and a.group_id == b.group_id:

      A group is DECLARED by the merchant. It is never inferred from a shared
      account, because a shared account is the thing being judged.

      ── The account index becomes many-to-many ──────────────────────

        build_account_index() -> Dict[str, Set[str]]      account -> {vendor_ids}

      Today it is Dict[str, str] built with `idx[acct] = v.vendor_id` in a loop,
      so a second vendor SILENTLY OVERWRITES the first, and a payout to a shared
      account is rejected for whichever vendor lost the race. The dict is the
      bug, not the rule that reads it.

      check_account_continuity then reads:
        owners == {this vendor}                    -> PASS
        owners share a declared group with this one -> PASS
        any other vendor                            -> FAIL, cross-contact reuse
        not on file anywhere                        -> WARN, new account

      ── Settlement history: a count, not per-payout rows ────────────

      The V2.6 policy asks two questions of an account — "has money actually
      arrived here" and "was it added long enough ago" — and settled_payout_count
      plus last_settled_on answer both. Per-payout rows would be a third CSV and
      a third thing to keep consistent.

      WHAT THAT FORECLOSES, stated so it is a decision and not an oversight: any
      later rule of the form "N settled payouts within the last M months", or
      anything about the AMOUNTS that settled. Both are plausible v3 signals. If
      one is wanted, that is a new generator pass — which is the whole reason
      this section exists.

      ── SEASONING_DAYS = 90 ─────────────────────────────────────────

      A tunable with a trade-off, not a discovered constant. Longer is safer
      against a planted account and harder on a vendor whose genuine second
      account is recent.

      90 because it must exceed a normal payment cycle. A vendor's account
      typically receives its first settled payout 30-60 days after being added,
      so a shorter window would make "seasoned" and "has a settled payout" the
      same condition asked twice, and the second test would buy nothing. And an
      attacker who holds a compromised mailbox for 90 days undetected is a
      materially rarer adversary than the one this system is built for.

      ── THE DATE PROBLEM, which is a real trap ──────────────────────

      Seasoning is a comparison against a date, and the naive version is
      `(datetime.now() - added_on).days >= 90`. That makes the dataset AGE: a
      case that holds today releases in six months, tests pass on the machine
      that wrote them and fail later, and nothing in the diff explains it.

      So: the generator emits a frozen AS_OF date and every case carries its own
      request_date. Seasoning is measured against the CASE's date, never against
      the clock. The dataset is then reproducible in 2030.

      ── data/cases_*.csv: stop encoding the policy's answer ─────────

      Today the case carries controls_existing_account: one bool meaning "a
      penny drop from the account on file would succeed". That was fine while
      every vendor had exactly one account, and it is the reason V2.6 was
      invisible for so long — the DATA already answered the question the policy
      is supposed to decide. With several accounts on file, "the account on
      file" is not a thing.

      Replace it with a description of the world, and let the code choose:

        REMOVE  controls_existing_account
        ADD     requester_controls_accounts   ;-joined account numbers the
                                              requester can actually send from
        ADD     request_date                  ISO date, the case's "now"
        ADD     destination_account_number    already present as
                                              proposed_account_number; keep

      The verifier then NAMES an account by policy, and the evaluator checks
      whether that named account is in requester_controls_accounts. If the
      policy names badly, the eval says so. Under the old column it could not:
      the answer was already in the data.

      For a legitimate vendor requester_controls_accounts is all of their
      accounts. For an attacker it is the accounts they planted, which is empty
      in every scenario except the planted-account one.

      ── New scenarios, and the column each one exists to exercise ───

        legit_group_shared_account   two vendors, one declared group, one shared
                                     account. REJECTED TODAY. group_id.
        legit_second_account         vendor with 2-3 accounts, payout to a
                                     non-primary one. Held forever today.
        legit_added_then_paid        an account added by an earlier accepted
                                     request, then paid. Makes ADD vs REPLACE
                                     testable end to end for the first time —
                                     the distinction R4's design rests on.
        fraud_planted_account        THE V2.6 ATTACK. Attacker got account B onto
                                     the master earlier (added_via=email_request,
                                     verified_by=unverified, recent, zero settled
                                     payouts), now requests C and can penny-drop
                                     from B. requester_controls_accounts=[B].
        fraud_first_contact          a vendor with no history at all. Nothing to
                                     compare against, and holding is correct.
        fraud_thread_hijack          a reply inside a real thread. Exists for
                                     V2.3; nothing reads it before then.

      fraud_planted_account is the one that matters most, because it is the only
      case in the corpus that will exercise the guard V2.1 added — a case that
      recommends rejection AND passes a channel. Until it exists, that guard is
      asserted by two constructed tests and measured by nothing.

      ── Deliberately NOT in this schema ─────────────────────────────

        per-payout settlement rows          see above
        account closure / reopening dates   status covers what the rules read
        a vendor-master audit log           the recursive problem README names;
                                            it is a v3 control, not a column
        message threading beyond a thread id V2.3 decides its own shape

V2.X  WHICH BUILDING BLOCK EACH ITEM TOUCHES

      (2.1 and 2.5 are DONE — Phase 1. 2.S is the schema decided in Phase 2.)

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

V2.4  ── DONE (Phase 6). Scored once, 249 cases, the full split.

        metric        v2 dev   v2 holdout   null      v1 holdout
        recall         100.0%      100.0%   100.0%        100.0%
        precision       85.9%       84.9%    84.3%         82.2%
        false BLOCK      0.0%        0.0%     0.0%          2.2%
        held            78.4%       77.9%   100.0%         52.0%
        false hold      14.9%       16.0%    16.8%             —

      End to end on all 249 with the live model: recall 100.0%, precision
      84.9%, false BLOCK 0.0% — IDENTICAL to the perfect-extraction bound,
      agreeing on the rule fired in 99.6% of cases. Zero extraction failures.
      Intent 100%, scope 100%, action 99.2%.

      The extractor cost NOTHING in outcomes, on both splits, through a
      provider it had never run on. That is now the third time this has been
      measured and it remains the most stable component in the system.

      THE V1 DEFECTS, on ground nothing was tuned against:
        corporate group sharing an account      12/12 allowed  (v1: REJECTED)
        legitimate second account               10/10 allowed  (v1: held forever)
        account added by an earlier request      8/8  allowed  (v1: unrepresentable)
        attacker drops from a planted account   12/12 held     (v1: RELEASED)

      60 holds carry a rejection recommendation and NONE is legitimate.
      Channel 2 was unavailable on 24 cases (9.6%), all escalated, none falling
      back to the callback.

      AND THE PART THAT MUST NOT BE DRESSED UP. Precision 84.9% against a null
      baseline of 84.3%; false hold 16.0% against 16.8%. On ACCURACY the rule
      table is barely distinguishable from holding every payout and phoning
      every vendor. v2 did not change that and was never going to: the value is
      the release rate (22.1% need no call), the triaged queue, and the
      elimination of the 2.2% of legitimate traffic v1 rejected outright.

      Recall of 100% on both splits is a CEILING, not a result — the dataset
      cannot fail. A corpus authored by the same people who wrote the rules
      shares their blind spots.

      Original scope: FRESH HOLDOUT. New seed, and add the scenarios v1 lacks: corporate
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

V2.5  ── DONE (Phase 1), not yet committed. check_account_status() returns
      INCONCLUSIVE for inactive. Effect on dev: R3 fired 93 -> 86, R4 56 -> 58,
      and BOTH legitimate cases v1 rejected left the adverse set entirely — the
      144 remaining recommendations are all fraud. So the two fixes are
      independent and both real: V2.5 removed the CAUSE of the false rejections,
      V2.1 removed the MECHANISM, and only the second makes it impossible for a
      future false positive to reach a rejection unattended.

      Original entry: carry over from the v1 holdout finding: account_status=inactive
      should be INCONCLUSIVE, not FAIL. It caused ALL FIVE false blocks across
      both splits, occurs on 2.0% of cases in both, and is UNCORRELATED with
      fraud (dev 7 fraud/4 legit; holdout 2 fraud/3 legit). It was a P0.2
      overcorrection — from ignoring the field entirely to treating it as a
      hard identity conflict. Deliberately NOT fixed in v1 because the holdout
      was already open and tuning against it would have invalidated the result.

