# PayeeProof

**A verified bank account and a verified account holder are not proof that a beneficiary change was authorized.**

PayeeProof is a pre-authorization decision layer for RazorpayX payouts. It intercepts at the `payout.pending` webhook — while the payout is frozen and no money has moved — verifies the *authorization provenance* of the proposed destination against the merchant's own vendor master, and calls Razorpay's native approve/reject endpoints.

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
        ALLOW  /  STEP_UP_VERIFY  /  BLOCK
                    |
   POST  /v1/payouts/{id}/approve   {"remarks": ...}
   POST  /v1/payouts/{id}/reject    {"remarks": ...}
   PATCH /v1/fund_accounts/{id}     {"active": false}
                    |
              Audit trail
```

**The LLM never decides** — that is the design intent, and it is how every path
except one currently behaves. It converts unstructured communication into structured
semantic evidence; a deterministic rule engine makes the money decision.

One path does not yet honour it. When the semantic layer reports `PAYMENT_FOLLOWUP`,
rule R2 returns ALLOW before any identity check runs, so a single model output can
release a payout. That is a known open flaw, not a design choice — see
[Known open flaws](#known-open-flaws).

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
src/llm_client.py       provider client — model auto-detect, 429 retry,
                        reasoning-model handling
src/extractor.py        the only LLM step; semantic layer + claims
src/decision_engine.py  deterministic policy; full rule table in docstring
src/verifier.py         callback to vendor-master contact; RazorpayX actions
src/pipeline.py         run_case() end to end -> audit dict
eval/ablation.py        semantic vs keyword ablation
data/generate_data.py   seeded generator (seed 42)
data/vendor_master.csv  120 vendors — the trusted record, committed
data/cases_dev.csv      558 labeled cases, committed
data/cases_holdout.csv  242 cases — gitignored, regenerate to reproduce
```

Quick start:

```
pip install -r requirements.txt
python data/generate_data.py   # vendor master + dev/holdout splits
python eval/ablation.py        # ablation; baseline half needs no API key

$env:GROQ_API_KEY="gsk_..."    # PowerShell
python src/pipeline.py         # hero case — refuses to run without the key
```

`src/pipeline.py` exits non-zero rather than printing a verdict when
`GROQ_API_KEY` is missing. Without a key, extraction fails, the payout is held,
and the hold looks superficially like a catch — so the demo declines to show one
rather than take credit for work the semantic layer never did.

---

## Evaluation methodology

Ground truth is assigned from independently authored scenario narratives, **not** derived from detector logic. Feature values are generated *from* each narrative afterwards, never the reverse.

- 120 synthetic vendors, 800 cases, stratified 70/30 — 558 dev, 242 holdout
- Four narratives: `fraud_easy`, `fraud_hard`, `legit_easy`, `legit_hard`
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

---

## Known open flaws

Found by audit, verified in the code, not yet fixed. Listed here because a control
that overstates its own guarantees is the failure mode it exists to prevent.

**The LLM can still produce an ALLOW.** `decision_engine.py` R2 returns ALLOW the
moment the extractor reports `intent == PAYMENT_FOLLOWUP`, before the Tier 1 checks
are constructed — no FAV, no GSTIN, no account continuity. A misclassification or a
successful prompt injection that reaches that label releases a payout with zero
identity validation.

**FAV `account_status` is never read.** It is carried on `FAVResult`, logged in the
audit record and printed in the summary, but no signal consumes it and the rule table
does not mention it. An `inactive` account with a name match of 99 passes. `unknown`
— meaning FAV was inconclusive — also passes, contradicting this project's own rule
that inconclusive must never resolve to ALLOW.

**The account checked is claimed, not actual.** Continuity is tested against the
account number the LLM read out of the email, while the payout's real destination is
never passed into the decision at all. At the `payout.pending` webhook the
authoritative destination is the payout's own fund account; wiring that in is a
prerequisite for the webhook handler, not a follow-up to it.

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

**No test suite.** An earlier revision of the project notes claimed 38 unit tests.
None exist. The claim has been removed rather than quietly left in place.

---

## Scope boundary

PayeeProof protects an **already-onboarded** vendor from having a payout redirected via a compromised or spoofed change request. It does not address a wholly fraudulent vendor being onboarded — that is onboarding fraud, a different pattern, and the vendor master is a trust boundary here.

The vendor master's own update path needs equivalent protection in production. The same principle applies recursively: a master-record update should be confirmed via the *existing* known contact, never via details supplied in the request.

---

## Platform constraints, handled transparently

- FAV is [unavailable in test mode](https://razorpay.com/docs/api/x/account-validation/) and is RazorpayX Lite only — a real integration constraint, not merely a sandbox one
- Approval Workflow is [unavailable in test mode](https://razorpay.com/docs/razorpayx/approval-workflow)
- Reverse Penny Drop is enabled on request, not by default

FAV results are replayed schema-faithfully from Razorpay's documented response shape — never presented as live calls. The decision layer is entirely PayeeProof's own logic.

**Failure recovery.** Razorpay auto-rejects payouts left pending beyond ~3 months, so a hold cannot sit forever; verification carries a bounded attempt count with explicit escalation. Extraction failure, FAV unavailability, and callback timeout all resolve to hold — never to auto-release.

---

## Sources

- [Fund Account Validation](https://razorpay.com/docs/x/fund-account-validation/)
- [Account Validation APIs](https://razorpay.com/docs/api/x/account-validation/)
- [Create Fund Account](https://razorpay.com/docs/api/x/account-validation/bank-account/create-fund-account/)
- [Fund Accounts — multiple per contact](https://razorpay.com/docs/x/fund-accounts/)
- [Reverse Penny Drop](https://razorpay.com/docs/x/fund-accounts/reverse-penny-drop/)
- [Approval Workflow](https://razorpay.com/docs/razorpayx/approval-workflow)
- [Payouts best practices](https://razorpay.com/docs/x/payouts/best-practices/)
- [FBI IC3 — Business Email Compromise](https://www.ic3.gov/PSA/2014/PSA140627.pdf)
