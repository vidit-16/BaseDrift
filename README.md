# PayeeProof

**A verified bank account and a verified account holder are not proof that a beneficiary change was authorized.**

PayeeProof is a pre-authorization decision layer for RazorpayX payouts. It intercepts at the `payout.pending` webhook — while the payout is frozen and no money has moved — verifies the *authorization provenance* of the proposed destination against the merchant's own vendor master, and resolves to Razorpay's native approve/reject endpoints.

**Precisely:** the decision layer, the webhook handler and the evaluation are real and run. The RazorpayX side is not connected — the engine emits the approve/reject/deactivate calls as action plans, and nothing in this repository executes them. See [What is real and what is simulated](#what-is-real-and-what-is-simulated).

Razorpay AI Buildathon — Track 2 (AI Risk Manager).

---

## The gap, in Razorpay's own words

Fund Account Validation compares the name returned by the bank against **"the name provided by the customer"** ([FAV docs](https://razorpay.com/docs/x/fund-account-validation/)). That name arrives in the `bank_account.name` field of the Create Fund Account request — supplied by whoever makes the request.

A finance team that receives a spoofed bank-change email and trusts the name in it supplies the attacker's preferred name into the check themselves. FAV returns a near-perfect match. Razorpay's own docs then note: *"if your user provides an account number by mistake which is not where the user wants the amount, the payout gets processed if the account number exists."*

Reverse Penny Drop doesn't close it either. RPD requires the account holder to send ₹1 by UPI from the account being verified — an attacker who owns that account completes the flow normally. Ownership proven. Authorization not.

| Control | Proves | Leaves open |
|---|---|---|
| Fund Account Validation | Account is real; name matches what *you submitted* | Whether you submitted the right name |
| Reverse Penny Drop | The account holder's identity, via their own UPI payment | Whether they are your vendor, or authorized this change |
| Approval Workflow | An internal role approved the payout | Whether the external vendor authorized the change |
| Source to Pay | Vendor onboarded in-portal with verified GSTIN | Whether an out-of-band email requesting a change is genuine |
| **PayeeProof** | **Authorization provenance of the change request** | — |

### The bypass PayeeProof survives

Razorpay's Approval Workflow can be disabled **for API payouts only**, and on disabling *"all the payouts in pending state are rejected automatically and the payouts are processed without approval."* A compromised integration with API credentials skips human review entirely. PayeeProof runs at the webhook layer and is unaffected by that toggle.

---

## Architecture

```
Out-of-band change request (email / invoice / message)
                    |
        +-----------v-----------+
        |  Semantic LLM layer   |  intent / action / scope / pressure
        |  evidence, not a      |  normalizes meaning, not keywords
        |  decision             |
        +-----------+-----------+
                    |
   +----------------+----------------+------------------+
   |                |                |                  |
Vendor master   FAV replay    Change lineage    Cross-contact
(trusted        (bank truth)  (add vs replace)  account reuse
 identity)
   +----------------+----------------+------------------+
                    |
          Authorization evidence
                    |
        Deterministic policy engine        <- no LLM here
                    |
            ALLOW  /  STEP_UP_VERIFY
                    |            \
                    |         (+ recommended_action="reject")
                    |
   POST  /v1/payouts/{id}/approve   {"remarks": ...}
   POST  /v1/payouts/{id}/reject    {"remarks": ...}    <- human confirms
   PATCH /v1/fund_accounts/{id}     {"active": false}   <- human confirms
                    |
              Audit trail
```

**The LLM never decides.** It converts unstructured communication into structured
semantic evidence; a deterministic rule engine makes the money decision.

**And neither does the rule engine reject anything.** The harshest outcome it can
reach on its own is a hold. Rules that once rejected now attach
`recommended_action="reject"`, and both the reject and the fund-account
deactivation are emitted flagged `requires_human_confirmation`. This costs
nothing in capture — a rejection prevents no fraud a hold does not, because the
money stays put either way — and it removes the only customer-facing failure the
system had. A hold costs a phone call; a rejection costs a vendor their payment.

Stated as the property that is actually enforced and tested: **an ALLOW always
requires the payout's real destination account to match the vendor master.** No
combination of model output can produce one on its own. The worst a hostile or
mistaken extraction can achieve is downgrading a rejection recommendation to a
plain hold — never a release.

This was not true until recently. R2 used to return ALLOW on the intent label alone,
before any identity check ran. See [What the audit found](#what-the-audit-found).

---

## The ablation result

The central claim is that the LLM does **semantic normalization**, not entity extraction. That is tested, not asserted.

**Method.** Messages expressing the same underlying request, classified into `(intent, action, scope)`. A keyword/regex baseline and the LLM see identical inputs and identical ground truth.

| Corpus | Keyword baseline | LLM semantic layer |
|---|---|---|
| v1 (vocabulary-cued) | 92.3% | — |
| **v2 (inference-only, 14 cases)** | **0/14 — 0.0%** | **14/14 — 100%** |
| Control false positives | **4/4** legit follow-ups flagged as a change | **0/4** |

Measured 2026-08-27 on `openai/gpt-oss-120b`.

Corpus v1's 92.3% was **our own methodology error**, kept in the repo as a record: the paraphrases were written first and the keyword trigger lists written afterwards to match them — the same evaluation-leakage failure the dataset methodology was designed to avoid, reappearing one layer up.

Corpus v2 removes trigger vocabulary entirely. No case states "replace", "add", "update", or any scope keyword:

- **Scope** is inferred from *which invoices are referenced* — an unpaid October invoice plus an ongoing retainer implies `OUTSTANDING_AND_FUTURE`.
- **Add vs replace** is inferred from *whether the existing account keeps a role* — this matters because RazorpayX permits [multiple fund accounts per contact](https://razorpay.com/docs/x/fund-accounts/), so treating every new account as a replacement is a real logic error.
- **Controls** contain account numbers, IFSC codes and change vocabulary while requesting no change to *this* vendor's destination (a third party's bank change, an internal process change, a change that already happened).

The baseline code is byte-identical across both runs. It was not weakened for v2.

**Stated honestly, and the 100% is the part to be careful about.** 14 hand-authored
cases is not a large sample, and a perfect score on it is weaker evidence than it
looks — it means the corpus has stopped discriminating between capable models, not
that the layer is flawless. The measurement has no headroom left to detect a
regression.

An earlier run of this same corpus scored 13/14, missing B3 — a case where our own
ground truth was arguably ambiguous and the model's stated reasoning was defensible.
That run used a different model, since retired. B3 now passes. **The score moved
because the model changed, not because anything in the corpus or the baseline
changed**, which is the honest reading: this number is model-dependent and should be
re-reported with the model id whenever it is quoted.

What the corpus still supports is the *contrast* — 0/14 against 14/14 on identical
inputs and identical ground truth — not the absolute figure.

**Which prompt this measures.** The ablation scores a semantics-only prompt —
`intent`, `action`, `scope`, `reasoning`. The production extractor folds those into a
single call that also returns seven claim fields and six pressure fields, so it is a
longer and harder prompt. The 14/14 belongs to the ablation's prompt and is indicative
of the approach rather than a measurement of the shipped one. Re-running the ablation
against the production prompt is open work.

Reproduce: `python eval/ablation.py` (the baseline half needs no API key)

---

## Layout

```
src/llm_client.py       provider client — the only module IN THE DECISION
                        PATH that talks to a provider; model auto-detect, 429
                        retry, reasoning-model handling. eval/ablation.py is
                        standalone and issues its own HTTP, honouring the same
                        variables. See "Choosing a provider" below.
src/extractor.py        the only LLM step; semantic layer + claims
src/decision_engine.py  deterministic policy; full rule table in docstring
src/verifier.py         two verification channels; names the account the penny
                        drop must come from; RazorpayX actions
src/pipeline.py         run_case() end to end -> audit dict
src/webhook.py          payout.pending handler; signature verification,
                        destination resolution, document correlation
src/webhook_app.py      ASGI entry point for uvicorn
src/dashboard.py        operator view: decisions, and the evidence behind each
src/demo.py             the two-minute demo: one real fraud case carried
                        end to end through the real code. Runs with or
                        without an API key and says which
src/webhook_demo.py     drives the real endpoint over real signed HTTP
src/triage.py           inbox funnel: dedupe -> ingest rules -> vendor
                        resolution (no model) -> classification
src/inbox_signals.py    mailbox facts -> Tier 2 signals that can hold a payout
                        and can never release one
src/investigator.py     the one agent loop; read-only tools, envelope-derived
                        arguments only
mcp/inbox_server.py     MCP inbox tools — all read-only, all scoped to one
                        merchant, both asserted structurally
COMPLIANCE.md           what production would have to satisfy, and why
                        anonymisation is not available to this design
NOTES.md                the working log: every v2 item, what it measured, and
                        what it does not show
tests/                  217 tests across 7 suites, none needing an API key
                        run them all: python tests/run_all.py
eval/rules_eval.py      decision-engine scoring vs baselines, no API key needed
eval/triage_eval.py     inbox funnel scoring, including the allowlist
                        counterfactual. No API key needed
eval/triage_classifier_eval.py
                        stage 4 measured against the free pre-read it
                        replaces. Needs a key; cached and resumable
eval/base_rates.py      what the measured rates mean per day at real
                        volumes. No API key needed
eval/extraction_eval.py what the extractor actually recovers, with caching
eval/ablation.py        semantic vs keyword ablation (needs a key)
data/render.py          case -> email renderer; leakage guard is a hard failure
data/generate_data.py   seeded generator (seed 20260829, AS_OF 2026-06-30)
data/generate_inbox.py  wraps the rendered cases in AP-inbox noise. Reads the
                        corpus, never regenerates it, so it cannot void an
                        extraction cache
data/vendor_master.csv  120 vendors — the trusted record, committed
data/vendor_accounts.csv  272 accounts with provenance, committed
data/cases_dev.csv      552 labeled cases, committed
data/cases_holdout.csv  248 cases — gitignored, regenerate to reproduce
data/inbox_dev.csv      7,176 messages, 7.7% of them change requests
```

## Setup

```
pip install -r requirements.txt
python data/generate_data.py    # vendor master, accounts, dev/holdout splits
python data/generate_inbox.py   # the AP inbox around those cases
```

Everything below this line runs with **no API key**:

```
python tests/run_all.py       # 217 tests across 7 suites
python eval/rules_eval.py     # rule scoring vs baselines
python eval/triage_eval.py    # inbox funnel, and the allowlist counterfactual
python eval/base_rates.py     # daily call volume vs the null baseline
python src/demo.py            # THE DEMO — one payout, end to end, ~2 min
python src/webhook_demo.py    # five signed scenarios over real HTTP
```

The dashboard:

```
$env:PAYEEPROOF_SEED_DEMO="1"          # populate it on startup
$env:RAZORPAY_WEBHOOK_SECRET="whsec_demo"
uvicorn webhook_app:app --app-dir src --port 8000
```

### Choosing a provider

The pinned model, `gpt-oss-120b`, is **open-weight** — roughly eighteen
companies run the same weights, and the model does not change when the provider
does. That is what keeps the data-localisation option in COMPLIANCE.md open, and
it is why the provider is configuration rather than code.

| variable | what it does |
|---|---|
| `PAYEEPROOF_BASE_URL` | provider root, OpenAI-compatible. Defaults to Groq |
| `PAYEEPROOF_API_KEY` | key for that provider. Falls back to `GROQ_API_KEY` |
| `PAYEEPROOF_MODEL` | pin a model id, skipping detection |
| `PAYEEPROOF_PROVIDER` | routing layers only: pin *which host* serves the model |
| `PAYEEPROOF_CALL_GAP` | seconds between calls. Default 7.0 |

**`PAYEEPROOF_CALL_GAP` is the one to change first.** 7 seconds exists to stay
under Groq's free-tier per-minute ceiling. Anywhere else it adds 93 minutes to
an 800-case run for nothing.

Windows, PowerShell — persists across reboots:

```
[Environment]::SetEnvironmentVariable("PAYEEPROOF_API_KEY", "sk-...", "User")
[Environment]::SetEnvironmentVariable("PAYEEPROOF_BASE_URL", "https://openrouter.ai/api/v1", "User")
[Environment]::SetEnvironmentVariable("PAYEEPROOF_CALL_GAP", "0.5", "User")
```

macOS or Linux:

```
export PAYEEPROOF_API_KEY="sk-..."
export PAYEEPROOF_BASE_URL="https://openrouter.ai/api/v1"
export PAYEEPROOF_CALL_GAP="0.5"
```

**A gotcha that cost an hour.** `SetEnvironmentVariable(..., "User")` writes to
the registry, and processes that are ALREADY RUNNING do not pick it up — including
the terminal you typed it in. Open a new one, or set `$env:NAME="..."` as well
for the current session.

Known-good settings, all running the same weights:

| provider | `PAYEEPROOF_BASE_URL` | model id | 800 calls |
|---|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-oss-120b` | ~$0.11 |
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` | ~$0.37 |
| Cerebras | `https://api.cerebras.ai/v1` | `gpt-oss-120b` | ~$0.64 |

Cerebras drops the `openai/` prefix; `MODEL_PREFERENCE` carries both spellings,
because without the second, detection there would silently fall through to a
different model.

Then the steps that cost API calls:

```
python src/pipeline.py                      # the hero case, one call
python eval/extraction_eval.py --split dev  # 552 calls, ~$0.09
python eval/triage_classifier_eval.py       # 405 calls, ~$0.05
python eval/ablation.py                     # semantic vs keyword ablation
```

`src/pipeline.py` exits non-zero rather than printing a verdict when no key is
set. Without one, extraction fails, the payout is held, and the hold looks
superficially like a catch — so the demo declines to show one rather than take
credit for work the semantic layer never did.

`eval/extraction_eval.py` caches every result keyed by the message hash, the
model and the prompt hash, so it is resumable and re-running it costs nothing.
It never caches a transport failure: a rate-limited run once persisted 201
"extraction failed" records that would have poisoned every later scoring pass
with a 56% failure rate that was really the network.

---

## The webhook, and the problem it has to solve

RazorpayX fires `payout.pending` while the payout is frozen. That event says
money is about to move — it does **not** say what document requested the
destination, and authorization provenance is the entire question here. Two
inputs are needed and only one arrives on the webhook.

The merchant's AP system supplies the other via `POST /documents`. Correlation
prefers an explicit `notes.payeeproof_document_id` on the payout, falls back to
the most recent document for that vendor inside a 30-day window, and otherwise
finds nothing.

**Finding nothing is not an error.** It is a true statement — nobody asked for
the destination to change — so it is expressed as `intent=PAYMENT_FOLLOWUP`,
tagged `evidence_source="no_document_supplied"` so no audit reader can mistake
it for something a model read, and handed to the same decision engine as
everything else. Rule R2 already encodes the right policy:

| destination | outcome | why |
|---|---|---|
| a known account for this vendor | ALLOW | nothing changed; nothing to authorize |
| an account never seen before | HELD | money moving somewhere new with nothing authorizing it |
| an account on file for another vendor | HELD + recommend reject | cross-contact reuse |

The handler decides nothing itself. It gathers evidence and calls `decide()`.

**The destination comes from the payout's own fund account**, resolved through
RazorpayX, never from an account number quoted in the request. A message naming
the vendor's genuine account while the payout points elsewhere is caught here,
at the integration boundary.

### Fail-safe by construction

The safe state is inaction. A pending payout stays pending until something
explicitly approves it, so every failure — bad signature, unknown fund account,
unknown vendor, unreadable document, a crashed process, PayeeProof being down
entirely — leaves the money where it is. There is no code path that releases a
payout on error, which is why a 500 is an acceptable response: Razorpay retries,
and nothing has moved in the meantime.

### Signature verification

HMAC-SHA256 over the raw body, per Razorpay's spec, with three details that are
each a real vulnerability if got wrong: the signature covers the bytes **as
received** rather than re-serialized JSON, the comparison is constant-time
(`hmac.compare_digest`), and malformed input returns False rather than falling
through. Events outside a 15-minute window are refused even with a valid
signature, and repeated event ids are not reprocessed.

A missing or zero `created_at` is refused rather than waved through. An earlier
version guarded that check with a truthiness test, which is falsy at zero and so
skipped freshness validation on exactly the events whose age could not be
established — caught by the test asserting that no input path releases a payout.

---

## Evaluation methodology

Ground truth is assigned from independently authored scenario narratives, **not** derived from detector logic. Feature values are generated *from* each narrative afterwards, never the reverse.

- 120 synthetic vendors, 800 cases, stratified 70/30 — 558 dev, 242 holdout
- Ten narratives: `fraud_easy`, `fraud_hard`, `fraud_compromised`,
  `fraud_mule`, `fraud_sim_swap`, `legit_easy`, `legit_hard`,
  `legit_rebrand`, `legit_add_account`, `legit_unreachable`
- `fraud_hard` carries `name_match_score` 85–100 — it passes every bank-level check
- `legit_hard` has a genuinely new account plus genuine urgency — the false-positive canary

**What holdout protection means here, precisely.** The generator is committed and
seeded (`random.seed(42)`), so both splits are reproducible byte-for-byte by anyone
who runs it. The holdout is therefore not secret and was never going to be — keeping
the CSV out of Git buys tidiness, not secrecy. The protection is a *process*
commitment: no rule or threshold is tuned against it, it is scored exactly once at
the end, and that run is reported with its size whatever the result. `cases_dev.csv`
and `vendor_master.csv` are committed; `cases_holdout.csv` is gitignored and
regenerated on demand.

**Known limitation, stated up front:** the narratives and the rules were authored by the same team, so they share one mental model of fraud. A blind spot would appear in both the exam and the student. The holdout measures generalization across cases, not across threat models.

### Why this project does not report an accuracy number

It would be 100%, and it would be worthless.

Every case carries `callback_reaches_known_contact`, which the generator sets
False for fraud and True for legitimate requests. The callback therefore
separates the classes perfectly by itself. **A pipeline running no rules at all
— holding every payout and phoning the vendor — scores 100% recall and 100%
precision on this dataset.** Accuracy cannot tell that system apart from this
one, so quoting it would be meaningless.

What the rules actually buy is measured against that null baseline:

| system | recall | precision | step-up | **false BLOCK** |
|---|---|---|---|---|
| null — hold everything, no rules | 100% | 86.3% | 100% | 0% |
| block everything | 100% | 43.2% | 0% | 100% |
| **PayeeProof** | **100%** | **86.0%** | **52.7%** | **0.6%** |

With both verification channels running, no fraud case in the dev split is
released — by PayeeProof or by the do-nothing baseline, which also catches
sim-swap once it can run a penny drop. So recall ties, and the rules' measurable
contribution is operational: **the same capture at half the verification cycles**,
while rejecting 0.6% of legitimate traffic.

**A rejected legitimate vendor is not the same event as a held one.** A BLOCK
rejects the payout and deactivates the fund account; a hold costs a phone call.
Reporting both as one false-positive number hides which one you are causing, so
they are tracked separately.

Reproduce: `python eval/rules_eval.py`, and `--sweep` for the threshold curve.

### What this evaluation still cannot tell you

Ten authored narratives, randomised within each — but the scenarios and the
rules come from one team's mental model of fraud, so a blind spot would appear
in both the exam and the student. Fraud is oversampled relative to real base
rates for statistical power. `eval/rules_eval.py` prints this under every run,
so no number leaves without it.

---

## What the audit found

A review of this repository found four ways the decision layer could release a payout
it should not have. All four are fixed and carry regression tests that fail against
the previous engine. They are documented rather than quietly patched, because a
control that hides its own near-misses is the failure mode it exists to prevent.

**The LLM could produce an ALLOW by itself.** R2 returned ALLOW the moment the
extractor reported `intent == PAYMENT_FOLLOWUP`, before the Tier 1 checks were
constructed — no FAV, no GSTIN, no continuity. Measured against the old engine: a
request with an unseen destination account, FAV reporting the account *inactive*, a
name match of 3/100, an attacker-controlled domain and urgency language returned
`ALLOW` with `payout_allowed=True`. A prompt injection that reached that one label was
a total bypass.

R2 now verifies the claim instead of accepting it. "Nothing is changing" is checked
against the real destination: known account → ALLOW, unknown → hold, another vendor's
account → block. Demonstrated end to end — the identical follow-up email allows when
the payout points at the vendor's known account and holds when it points anywhere
else.

**FAV `account_status` was never read.** It was carried on `FAVResult`, logged, and
printed, but consumed by nothing. A change request against an account the bank
reported **inactive**, with a name match of 99, returned `R7_all_clear` — "all
identity checks passed". It is now a Tier 1 signal: active passes, inactive fails,
unknown warns, because an inconclusive FAV is not a clean one.

**The account checked was claimed, not actual.** Continuity tested the account number
the LLM read out of the email; the payout's real destination was never passed into the
decision at all — the old `decide()` had no parameter for it. A message naming the
vendor's genuine account while the payout pointed elsewhere passed cleanly. The
resolved destination now comes from the payout's fund account, the message's claim is
a labelled fallback for offline analysis only, and every audit record states which of
the two was validated.

**Contextual signals alone could reject a legitimate vendor.** R4 blocked on
`REPLACE + new account + any 2 Tier-2 warns`. Both inputs are true of an
ordinary legitimate bank change — a new account is what changing banks *means*,
and urgency plus an unfamiliar domain is what an acquired company's finance team
looks like. It also contradicted the rule table's own stated invariant that
Tier 2 is supporting evidence and never decisive alone.

Measured: **it rejected 15.8% of all legitimate traffic** — 49 of 60
acquisition/rebrand cases — and no threshold repaired it. Tightening to 4 warns
cut false blocks to 0.6% but dropped recall to 85.4%, level with doing nothing.

R4 now requires evidence of *deliberate impersonation* — currently a typosquat
domain, carried as an extensible `deception` flag on `Signal` — corroborated by
at least one contextual signal. False blocks fell to **0.6%** and precision rose
from 74.5% to 87.5%.

The alternative was tested rather than assumed: treating any domain that embeds
the vendor's name as deception recovers recall to 97.9% but returns false blocks
to 15.8%, because a rebrand embeds the vendor's name too. That ambiguity is real
rather than a detector gap — see the limit stated below.

**The injection filter missed the two best hiding places.** `sanitize()`
enumerated eight characters — zero-widths, soft hyphen, the LTR/RTL *marks*,
BOM — and its docstring called them "the characters used to hide instructions
inside documents". Two classes survived it:

- **U+202A–202E and U+2066–2069**, the bidirectional embeddings, overrides and
  isolates. An RLO makes text render in an order entirely different from how it
  reads.
- **U+E0000–E007F**, the Unicode Tag block. Wholly invisible, and the basis of
  ASCII smuggling — arbitrary instructions riding inside innocuous text.

Enumerating characters means the list is only ever as good as the last threat
someone remembered, so filtering is now by Unicode **category** (`Cf` and `Cc`,
plus the tag block explicitly), which closes the class rather than patching it.
Newline and tab are preserved because they carry document structure.

The count of what was removed is now reported rather than discarded: a
legitimate invoice email contains none of these, so their presence is evidence
about the document. Oversized documents are flagged too — padding the front
pushes the real request past the size cap, and a model that read only part of a
document is inconclusive, not clean. Neither feeds a rule yet; the generator
emits no such cases, and rules do not change without dataset coverage.

**Missing data was counted as evidence of fraud.** `Signal` had three states,
and `WARN` was carrying two incompatible meanings: "I checked this and it looks
wrong" and "I could not check this". A message that simply omitted an amount
earned a Tier-2 risk signal, which fed R4's BLOCK threshold — so a case could be
pushed toward rejection for carrying *less* information rather than worse
information. That was the third of the three warns on the hero case.

Signals now have four states, with `INCONCLUSIVE` distinct from `WARN`. Both
still hold a payout — "couldn't check" must never read as "clean" — but only
adverse evidence can contribute to a rejection. The hero case still blocks, now
citing two real signals instead of three padded ones.

**Hedge detection failed open on spelling.** `check_gstin` decided PASS-versus-WARN by
testing `hedged_fields` against the exact tuple `("gstin", "proposed_gstin")`. The
model emits `gst_number` and `gst` for the same concept, and both were silently
missed — and a missed hedge means one fewer WARN, biasing toward release. The hero
case demonstrated it live, reporting `gstin PASS` on "should be the same as before".
Matching is now on the concept, and that same case now correctly warns.

---

## Known open flaws

**A compromised callback was unrecoverable from evidence — so a second channel
was added.** When the attacker controls the vendor's phone as well as the mail,
the callback confirms the fraud. 17 cases released on the dev set, and no amount
of rule tuning could reach them: they are identical to a genuine rebrand on every
observable signal.

The fix is not another heuristic. A phone number can be taken; **the account we
have already been paying cannot** — moving money away from it is the entire point
of the attack. So the second channel is a **Reverse Penny Drop from the account
already on file**: send ₹1 from where we have been paying you.

The obvious rule for combining them is wrong. SIM-swap fraud has the callback
*passing* — the attacker answers the phone — so "either channel confirms" releases
exactly the cases the channel was added for. The penny drop is therefore
authoritative and the callback corroborates, mirroring the Tier 1 / Tier 2 split
in the rule table.

Measured on the dev set:

| | callback only | with the penny drop |
|---|---|---|
| `fraud_sim_swap` released | **17** | **0** |
| `legit_unreachable` held | **30** | **0** |
| recall | 92.9% | **100%** |
| false BLOCK | 0.6% | **0.6%** |
| false hold | 9.5% | 11.7% |

It closes the gap in both directions: no sim-swap case survives it, and thirty
genuine vendors who simply could not answer a phone are no longer held for one.

**The honest cost.** A vendor who genuinely closed their old account fails the
penny drop through no fault of their own and lands beside the attacker in a state
the evidence cannot separate. Both are held for a human rather than released —
false holds rose 9.5% → 11.7%, and **rejections did not move at all**. That is the
trade taken deliberately: a hold is recoverable, a release is not.

**And it raises the floor for everyone, including doing nothing.** The null
baseline also reaches 100% recall now, because holding every payout and running
both channels catches sim-swap too. So the rules' measurable contribution reverts
to what it was before: the same capture at **half the verification cycles** —
52.7% against 100%.

**FIXED IN v2 (uncommitted working tree).** The limitation below is the v1
behaviour and the reason for the rebuild. `vendor_accounts.csv` now carries each
account with its own provenance — `added_via`, `verified_by`,
`settled_payout_count` — `build_account_index()` returns a set of owners rather
than overwriting, and `group_id` distinguishes a declared corporate group from
the mule pattern. Measured on the v2 dev split: corporate groups sharing an
account are allowed (21 of 22), and a payout to a legitimate second account is
allowed (21 of 21) where v1 held it on every payout, forever.

**The vendor master holds one account per vendor, and real ones do not.**
`VendorRecord` has an `additional_accounts` field and `all_known_accounts()`
reads it — but nothing populates it, no CSV column carries it, and all 120
vendors have exactly one account. It is a coded capability with no data behind
it, the same shape `account_status` had before it was fixed.

Two things follow, and the first is reproducible today. `build_account_index()`
maps account to vendor with a plain assignment, so when two vendors share an
account the second **silently overwrites** the first — and a payout to that
shared account then fires `R2c_followup_destination_conflict` and is **rejected**.
`decision_engine.py`'s own comment says sharing an account across contacts is
legitimate for corporate groups; the code blocks it. Second, a vendor with a
genuine second account — separate divisions, a collections and a refunds
account — gets a hold on every payout to it, permanently, because the master
records only one.

It also makes `legit_add_account` incoherent as a scenario: the request to add
an account is modelled, the resulting state never is, so ADD versus REPLACE
cannot be tested end to end even though R4's design rests on the distinction.

The fix is a separate `vendor_accounts` table carrying each account's own
attributes, a many-to-many index, and an explicit group id so that a shared
account inside a declared group is distinguishable from the mule pattern. The
important column is **`verified_by`**: the vendor master is the root of trust
for every check here, so an account that entered it without verification is
worthless as an anchor — the destination would be checked against a record an
attacker could have written. Scoped in NOTES.md as V2.0.

**Extraction is not reproducible run to run.** The semantic layer is called at
`temperature=0.0`, but repeated calls on the identical hero email return different
`hedged_fields` values — `['gstin']`, `['proposed_gstin']`, and
`['proposed_account_number', ...]` were all observed across six runs. The final
decision was stable at `R4_bec_pattern` in all six, so the demo does not wobble, but
the underlying signals do. This matters most for the forthcoming scorer: a 558-case
dev run is not reproducible, and a threshold tuned on one run may not hold on the
next. Any reported eval figure will need a stated run count, not a single number.

**Hedge detection is coupled to free-text model output.** `check_gstin` decides
PASS-versus-WARN by testing `hedged_fields` against the exact tuple
`("gstin", "proposed_gstin")`. The model emits at least three different spellings for
the same concept, so the hedge is silently missed whenever it picks one not in that
tuple — observed once on the hero case, which reported `gstin PASS` on wording
("should be the same as before") that is plainly hedged. The check needs to stop
string-matching against an open vocabulary.

**Not yet connected to Razorpay.** Covered in full below — the decision layer
is real, the integration is not.

---

## What the extractor actually costs

`eval/rules_eval.py` assumes perfect extraction. This measures the gap.
`data/render.py` turns each case row into the message a finance team would have
received; `eval/extraction_eval.py` runs the real model over them and compares
the result against the rules-only upper bound.

**The leakage guard comes first, because the README already records this project
making that exact mistake once.** Ablation corpus v1 scored the keyword baseline
at 92.3% because the paraphrases were written first and the trigger lists after.
Rendering an eval corpus is the same trap one layer down. So `BANNED_VOCABULARY`
is imported from the baseline's own trigger lists — retyping it would let the two
drift — and a hit is a hard failure that stops rendering, not a warning. The
baseline is then re-run over the finished corpus:

```
keyword baseline over the rendered corpus:  0 / 556 = 0.0%
```

Meaning has to be inferred from these messages, not pattern-matched. (Its
intent-only figure of 79.5% is the baseline's degenerate rule showing through —
it labels anything containing an account number as a change, so it gets all 442
changes right and all 114 follow-ups wrong.)

### Measured — 60 stratified cases, 1 run, `openai/gpt-oss-120b`

| | |
|---|---|
| intent | 100% |
| scope | 100% |
| action | 96.6% |
| account · IFSC · domain · amount | 98.3% · 98.3% · 100% · 100% |
| urgency (precision / recall) | 94.4% / 100% |
| channel manipulation | 100% / **70.6%** |
| extraction failed | 1.7% |

**End to end the extractor costs nothing on this sample:**

| | recall | precision | false BLOCK | same rule as ideal |
|---|---|---|---|---|
| rules-only upper bound | 86.7% | 81.2% | 0.0% | — |
| **with real extraction** | **86.7%** | **81.2%** | **0.0%** | **98.3%** |

That is not luck, it is the architecture doing what it was built to do. Identity
never comes from the extracted claims — the destination is read from the payout's
own fund account — so a misread account number cannot move a decision. And the
signals the model *does* miss are corroborating ones: channel manipulation at
70.6% recall can downgrade a BLOCK to a hold, never a hold to a release.

**Two findings from building it, both mine rather than the model's.** Scoring
initially showed 90% on account numbers; every miss was a follow-up where the
model correctly returned *nothing proposed*, because the field is
`proposed_account_number` and a message restating where payment has always gone
proposes nothing. The ground truth was wrong. Separately, the model returns the
full `accounts@vendor.com` roughly 8% of the time where a domain was asked for —
which `check_domain` would read as a mismatch for a domain that matches, and
which would defeat lookalike detection entirely, since the edit distance is
computed on registrable labels. Normalising that took domain recovery from 91.7%
to 100%.

Caveats that travel with these numbers: 60 of 556 cases, one run, one model. The
extractor is not reproducible run to run, so a single pass is one sample —
`--runs N` reports the spread.

---

## The operator view

`http://localhost:8000/` lists decisions newest first; each one opens onto the
evidence behind it. The point is not that the system returns a verdict — it is
that **every verdict is attributable**. A held payout is somebody's money, and
the person holding it has to be able to say why:

```
BLOCK   R2c_followup_destination_conflict

Destination account      999988887777
Destination came from    razorpay_fund_account      <- not the email
Change request           none on file
Evidence source          no_document_supplied
Semantic reading         PAYMENT_FOLLOWUP / NONE / NONE

TIER 1 — identity, against the vendor master
FAIL  account_continuity  account 999988887777 is already on file for a
                          different vendor (VEND0123) — cross-contact reuse
                          source: vendor_master_crosscheck

POST  /v1/payouts/pout_fa_mule/reject
PATCH /v1/fund_accounts/fa_mule        [needs human confirmation]
```

**It deliberately shows what the webhook response withholds.** The reply to
Razorpay carries no audit record and no reason string, because reason strings
embed account numbers and that reply crosses the public internet. The dashboard
has a different audience — an operator inside the merchant — so it shows the
signal table. That difference is deliberate, and it means this endpoint needs
authentication in front of it wherever it is actually deployed, exactly as
`/documents` does.

The store starts **empty** by default, which resolves no fund accounts and
therefore holds every payout. That is the right default for a control whose safe
state is inaction: a deployment that has not been given vendor data must not
begin approving things. `PAYEEPROOF_SEED_DEMO=1` loads the demo fixtures, and
has to be set deliberately.

---

## The held-out result — v2

**248 cases, the full split**, both rules-only and end to end with the live
model.

**The holdout was scored twice, and both runs are below.** The first corpus was
generated wrong — see *Why there are two runs* — so its numbers describe a
dataset that no longer exists. Nothing was tuned from what it showed.

| | v2 dev (552) | **v2 holdout (248)** | null baseline |
|---|---|---|---|
| recall | 100% | **100%** | 100% |
| precision | 85.1% | **87.1%** | 85.8% |
| **false BLOCK** | 0.0% | **0.0%** | 0.0% |
| held | 79.3% | **79.4%** | 100% |
| false hold | 16.6% | **14.2%** | 15.7% |

End to end, with the real model on all 248: **recall 100%, precision 87.1%,
false BLOCK 0.0%** — identical to the perfect-extraction upper bound, agreeing
on the rule fired in **98.8%** of cases. Zero extraction failures. Intent 99.6%,
scope 99.6%, action 98.8%; all three misreads are `ADD` read as something else,
and none changes an outcome.

### Why there are two runs

While verifying a figure for this README, one would not reconcile:
`vendor_accounts.csv` held 213 rows where the text said 272. The cause was a
variable name. A domain de-duplication loop used `n` as its collision counter,
and `n` was already `generate_vendor_master`'s vendor-count parameter — so it
left the loop as `2`, and `range(max(1, n // 6))` built **one** declared
corporate group instead of twenty.

That first corpus therefore had a single group of three vendors sharing one
account, and all 37 `legit_group_shared_account` cases were drawn from that one
configuration. The result reported from it was true and close to meaningless.

**It was not a local defect.** One group instead of twenty is a different number
of RNG draws, so the stream shifted and every subsequent value changed. Between
the two dev splits, **zero rows are byte-identical** and only 379 case ids even
overlap. The first corpus was a different dataset, not a narrower one.

| | run 1 (defective corpus) | run 2 (corrected) |
|---|---|---|
| holdout size | 249 | 248 |
| recall | 100% | 100% |
| precision | 84.9% | 87.1% |
| false BLOCK | 0.0% | 0.0% |
| false hold | 16.0% | 14.2% |
| declared groups in the master | **1** | **20** |
| groups sharing an account | **1** | **14** |

**Every one of the 216 tests passed on the broken corpus.** Both evals ran
clean, the generator was byte-identical across two runs, and the leakage guard
reported 0/551. One declared group *is* a structurally valid vendor master.
Nothing asserted that the corpus contained *enough* of a scenario to measure it
— this project's own recurring finding, a coded capability with no data behind
it, turned on the data itself.

The guard added in response puts a **diversity floor** on the master rather than
a shape check: unique domains, at least 10 declared groups, at least 5 shared
accounts, and every shared account confined to a single group. It fails on the
broken corpus where every previous assertion passed.

Scoring a holdout twice is an exception to a claim this project makes, so it is
recorded rather than absorbed. What makes it defensible: the regeneration was
forced by a figure that would not reconcile, not by a result anyone disliked;
nothing was tuned from the first holdout; and the second corpus is a different
dataset rather than a second look at the same one.

**The three v1 defects stay closed on data nothing was tuned against:**

| scenario | v1 behaviour | v2 holdout |
|---|---|---|
| corporate group sharing an account | **rejected** | 9/10 allowed |
| vendor's legitimate second account | held on every payout, forever | 9/9 allowed |
| account added by an earlier accepted request | could not be represented | 10/10 allowed |
| attacker penny-drops from a planted account | **channel 2 confirms the fraud** | 13/13 held |

The single held group case is an unrelated Tier-1 inconclusive, not a group
rejection. Those 10 cases are drawn from 14 genuinely sharing groups, which is
the difference the corpus fix made: in run 1 the same result came from one.

**And the customer-facing failure is gone.** v1 rejected **2.2%** of legitimate
holdout traffic outright. v2 rejects nothing at all — not as an accuracy result
but *by construction*, because no rule can reach a rejection any more. The 57
holds that carry a rejection recommendation are **all fraud**; none is
legitimate.

### Read these numbers honestly

**Precision 87.1% against a null baseline of 85.8%, and false hold 14.2%
against 15.7%.** On accuracy, the rule table is barely distinguishable from
holding every payout and phoning every vendor. That was true in v1, it was true
on both v2 corpora, and no amount of work in v2 changed it.

What the rules actually buy is the **release rate**: 20.6% of payouts clear with
no phone call at all, and the holds are triaged rather than being one pile — a
BEC case and a routine unfamiliar-account hold are distinguishable in the queue.

**Recall of 100% is a ceiling, not a result.** It means the dataset cannot fail,
not that the system cannot. A synthetic corpus authored by the same people who
wrote the rules will share their blind spots; a fraud pattern neither the
scenarios nor the rules imagine appears in neither.

**What a "fresh" holdout can honestly claim.** v1's holdout was seen, and what
it showed shaped v2 — the `inactive` fix and the no-reject decision both came
from reading it. A new seed does not un-see that. What it buys is that the parts
of v2 that matter most — multi-account vendors, corporate groups, the
verification account, the planted-account attack — are tested on ground nothing
has ever been tuned against. That is a smaller claim than "fresh holdout"
implies, and it is the true one.

<details>
<summary>v1's holdout, for comparison</summary>

**244 cases**, rules-only, plus **112 of those 244** end to end with the live
model before the daily token quota stopped it.

| | v1 dev (556) | **v1 holdout (244)** | v1 end-to-end (112) |
|---|---|---|---|
| recall | 100% | **100%** | **100%** |
| precision | 86.0% | **82.2%** | 79.0% |
| step-up | 52.7% | **52.0%** | — |
| false BLOCK | 0.6% | **2.2%** | 1.6% |

**Capture and operational cost generalised.** 100% of fraud held on unseen data,
and the step-up rate moved 0.7 points. On the 112 scored end to end, the real
model produced **identical outcomes to perfect extraction**, agreeing on the
rule fired in 97.3% of cases — the extractor cost nothing on data it had never
seen.

**Precision degraded, and the cause is a single defect.** All five false blocks
across both splits have one origin: `account_status = inactive` routed to a
Tier-1 FAIL and therefore a rejection. FAV reports inactive on 2.0% of cases in
both splits and it is **uncorrelated with fraud** (dev 7 fraud / 4 legit;
holdout 2 fraud / 3 legit), so it rejects legitimate traffic for a signal that
carries no fraud information.

It was our own overcorrection: `account_status` had previously been read by
*nothing*, and the fix made it a hard conflict rather than a reason to hold.

**It is deliberately not fixed.** The holdout has been opened, and changing a
rule in response to what it showed would turn this into a development number.
It was the first item in the v2 scope, and V2.5 fixed it — see the v2 result
above, where false blocks are 0.0% on both splits.

</details>

---

## How often does this actually fire?

Worth being concrete, because the answer is *not* "constantly", and the case for
the system does not rest on volume.

`python eval/base_rates.py` derives this from the measured corpus rather than
assuming it. The corpus splits cleanly into traffic that requests a destination
change and traffic that does not, and the two behave nothing alike:

| | n | held |
|---|---|---|
| routine payout, no change requested | 167 | **1.2%** |
| change request, legitimate | 243 | 25.9% |
| change request, fraudulent | 390 | 100% |

Real traffic is overwhelmingly the first row, and the null baseline holds all of
it. At 20,000 payouts/day with 0.2% carrying a change:

| per day | PayeeProof | hold everything |
|---|---|---|
| released with no call | **19,750** | 0 |
| held, a human must act | **250** | 20,000 |
| of those, actually fraud | 0.8 | 0.8 |
| legitimate payments cancelled | 0 | 0 |

**Both catch every fraud case in this corpus. One asks for 250 phone calls a day
and the other asks for 20,000.** That factor of 80 is what a precision ratio
hides: on a corpus that is half fraud the two look three points apart; on
traffic anyone actually runs, one is a staffed desk and the other is impossible.

**And 250 calls a day is not nothing** — about 21 hours of work, so a team. The
system does not remove the work, it makes the work possible and points it at the
right payouts. Fraud is 0.32% of what gets held, which is precisely the ratio at
which people start rubber-stamping. That the queue is *sorted* — a rejection
recommendation attached to the cases carrying real evidence — matters more at
that ratio than any accuracy figure here.

**The weakest input, stated plainly:** the 1.2% routine hold rate rests on **two
events** across both splits. Every daily figure scales linearly with it and the
confidence interval is wide. `--routine-hold` overrides it so anyone can see how
sensitive the conclusion is.

**One real attempt every few weeks, even at scale.** A person could make those
phone calls. The argument for this system is not that a human cannot keep up —
it is that **a human does not know which call to make.** BEC works precisely
because the message looks routine; the clerk skips the call because nothing
seemed wrong, not because they were busy.

So the value is enforcement, not throughput: the payout is held by default and
something has to actively release it. `"Our quarter closes Friday"` is written
to make a person skip the check. A rule engine does not feel deadlines.

This also reframes precision. At these volumes you hold roughly five legitimate
requests for every fraudulent one — a ratio that sounds alarming and amounts to
one unnecessary phone call every few days. **Absolute volume is the operational
metric here, not the ratio**, which is also why the hold-versus-reject
distinction matters so much: holding costs a call, rejecting costs a vendor.

---

## What this data does and does not establish

Every number here comes from a corpus this project generated, with scenarios and
rules written by the same person. That is a real limitation and it cannot be
argued away, so the useful thing is to be precise about which claims depend on
the data and which do not.

**Structural — true regardless of what data you run:**

| property | enforced by |
|---|---|
| No rule can reject a payout unattended | no `BLOCK` outcome exists in the engine; a test asserts the literal is absent |
| Inbox evidence can hold a payout, never release one | every inbox signal is WARN or INCONCLUSIVE; `decide()` rejects anything else before any rule runs |
| The model cannot reach an approve endpoint | it is called once, returns JSON, and holds no tools |
| The destination checked is the payout's, not the email's | `resolve_destination()`; a blank value returns an error rather than falling back to the claim |
| A shared account inside a declared group is not the mule pattern | `same_group()` requires a non-empty id, so two blanks never match |
| Every decision names the model *and* the host that ran it | `served_by` in the audit record |
| The safe state is inaction | no error path releases a payout; an unconfigured deployment holds everything |

These are properties of the architecture. A different dataset does not move
them, and a hostile one cannot either.

**Measured on synthetic data — and therefore uncertain:** recall, precision,
hold rates, the false-hold rate, and every figure in the held-out result above.
Recall of 100% in particular is a *ceiling*, not an achievement: it says the
corpus cannot fail, not that the system cannot.

**Measured on hand-written adversarial cases, not generated ones:** the
[ablation](#the-ablation-result). Its fourteen cases were written to be
semantically obvious and lexically misleading, and the keyword baseline scores
0/14 against the model's 14/14. That is the one accuracy claim here that does
not rest on the generator.

**What would actually strengthen this**, in order: shadow mode against real
traffic at a willing merchant, which is the only thing that produces a genuine
false-positive rate; a corpus authored by someone who did not write the rules;
and a red-team pass by someone trying to get a payout released. None of those is
a code change, which is why none of them is in this repository.

---

## What is real and what is simulated

Stating this plainly because the difference is easy to blur, and a fraud control
that overstates its own deployment status is exactly the failure mode it exists
to prevent.

**Real, and runs:** the decision engine and its rule table; the semantic layer
against a live model; the webhook handler including HMAC verification, replay
and idempotency handling; document correlation; the rules evaluation and the
ablation; the operator dashboard; the inbox triage funnel and its MCP tool
layer; 217 tests.

**Simulated:** every RazorpayX boundary. `Store` stands in for fund-account and
vendor lookups that would be API reads. FAV results are replayed
schema-faithfully — it is unavailable in test mode and is RazorpayX Lite only.
The callback outcome comes from scenario ground truth. `razorpay_actions()`
returns action *plans*; **nothing here calls Razorpay.**

### What production would additionally require

Not blockers for the evaluation work, but blockers for any claim of live
operation:

- **The handler must become asynchronous.** It currently calls the model inline,
  and `llm_client` permits a 45-second request plus retries. Razorpay expects a
  2xx well inside a few seconds. The shape it needs: verify HMAC → durably claim
  the event → return 2xx → a worker resolves the fund account, document and FAV,
  decides, writes the audit, and an idempotent executor performs the actions.
- **Durable idempotency.** Dedupe is an in-memory dict today, so a restart
  forgets it and a redelivery would be decided twice. This needs a uniqueness
  constraint in real storage, and the claim must not be marked complete until
  the work finishes — otherwise a crash mid-decision leaves an event
  permanently "processed" and its payout permanently pending.
- **`POST /documents` needs authentication.** It currently accepts a document
  for any vendor from anyone who can reach it. That cannot release a payout —
  the destination check still governs — but it is a denial-of-service on the
  payout queue, and the endpoint needs merchant authentication, vendor
  authorization, size limits and immutable server-side storage.
- **Deactivation must stay behind human review.** BLOCK emits the fund-account
  deactivation flagged `requires_human_confirmation`. Rejecting one payout is
  recoverable; deactivating the destination is not, and at the measured
  false-block rate that would be roughly one legitimate vendor in 170 losing a
  destination on a decision nobody reviewed.
- **The Payout Approval API is not enabled by default** — it requires Technology
  Partner/OAuth access, which is a commercial prerequisite rather than a code
  one.

**Compliance is its own document.** [COMPLIANCE.md](COMPLIANCE.md) sets out the
regime that applies, what this design already satisfies, and what it does not.
The short version: the usual answer — remove the personal data — is unavailable
here, because the personal data *is* the input. The model has to read the vendor's
name and account number to know a change is being requested at all. So the
controls are boundary, provenance and retention rather than anonymisation, and
the largest open question is that inference currently leaves the country. The
pinned model is open-weight and `src/llm_client.py` is the only place that talks
to a provider, so bringing inference in-country is a one-file change.

---

## Scope boundary

PayeeProof protects an **already-onboarded** vendor from having a payout redirected via a compromised or spoofed change request. It does not address a wholly fraudulent vendor being onboarded — that is onboarding fraud, a different pattern, and the vendor master is a trust boundary here.

The vendor master's own update path needs equivalent protection in production. The same principle applies recursively: a master-record update should be confirmed via the *existing* known contact, never via details supplied in the request.

---

## Platform constraints, handled transparently

- FAV is [unavailable in test mode](https://razorpay.com/docs/api/x/account-validation/) and is RazorpayX Lite only — a real integration constraint, not merely a sandbox one
- Reverse Penny Drop is enabled on request, not by default

### The integration point cannot be exercised in a sandbox

This is the sharpest constraint here and it is worth stating exactly, because
"nothing calls Razorpay" otherwise reads like a gap someone chose not to close.

[RazorpayX Test Mode](https://razorpay.com/docs/x/dashboard/test-mode/) says:

> The Approval Workflow is not available in the test mode. This means the
> `pending` and `rejected` states are not available in the test mode.

Payouts created in test mode start in `processing`, or `queued` if the balance
is short. So:

| | in test mode |
|---|---|
| a payout reaching `pending` | **impossible** — the state does not exist there |
| receiving `payout.pending` | never fires; there is nothing to fire on |
| `POST /payouts/{id}/approve` and `/reject` | nothing to act on — they operate on pending payouts |

**Both halves of the loop are blocked, not just the inbound one.** The event
itself is real and applies to all payouts; what is unavailable is any way to
reach the state that triggers it without a live account and real money.

A partial integration — replay the event inbound, execute approve/reject
outbound against test mode — does not work either, for the same reason. It was
considered and is ruled out.

What that leaves: this system's control point requires **live mode with Approval
Workflow enabled**, on a real RazorpayX current account, with Payout Approval
API access, which is a Technology Partner or OAuth arrangement rather than a
default. Those are commercial and onboarding prerequisites, not engineering
ones.

So the simulation here is not a shortcut taken in place of an integration that
was available. It is the only thing that *can* be built before an account
exists, and the honest next rung is shadow mode against a willing merchant —
deciding nothing, logging what it would have done.

FAV results are replayed schema-faithfully from Razorpay's documented response shape — never presented as live calls. The decision layer is entirely PayeeProof's own logic.

**Failure recovery.** Razorpay auto-rejects payouts left pending beyond ~3 months, so a hold cannot sit forever; verification carries a bounded attempt count with explicit escalation. Extraction failure, FAV unavailability, and callback timeout all resolve to hold — never to auto-release.

---

## Sources

- [Fund Account Validation](https://razorpay.com/docs/x/fund-account-validation/)
- [Account Validation APIs](https://razorpay.com/docs/api/x/account-validation/)
- [Create Fund Account](https://razorpay.com/docs/api/x/account-validation/bank-account/create-fund-account/)
- [Fund Accounts — multiple per contact](https://razorpay.com/docs/x/fund-accounts/)
- [Reverse Penny Drop](https://razorpay.com/docs/x/fund-accounts/reverse-penny-drop/)
- [Approval Workflow](https://razorpay.com/docs/x/manage-teams/approval-workflow/)
- [RazorpayX Test Mode](https://razorpay.com/docs/x/dashboard/test-mode/) — why the integration point cannot be exercised in a sandbox
- [Payouts best practices](https://razorpay.com/docs/x/payouts/best-practices/)
- [FBI IC3 — Business Email Compromise](https://www.ic3.gov/PSA/2014/PSA140627.pdf)
