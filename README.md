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

**The LLM never decides.** It converts unstructured communication into structured semantic evidence. A deterministic rule engine makes every money decision.

---

## The ablation result

The central claim is that the LLM does **semantic normalization**, not entity extraction. That is tested, not asserted.

**Method.** Messages expressing the same underlying request, classified into `(intent, action, scope)`. A keyword/regex baseline and the LLM see identical inputs and identical ground truth.

| Corpus | Keyword baseline | LLM semantic layer |
|---|---|---|
| v1 (vocabulary-cued) | 92.3% | — |
| **v2 (inference-only, 14 cases)** | **0/14 — 0.0%** | **13/14 — 92.9%** |
| Control false positives | **4/4** legit follow-ups flagged as a change | **0/4** |

Corpus v1's 92.3% was **our own methodology error**, kept in the repo as a record: the paraphrases were written first and the keyword trigger lists written afterwards to match them — the same evaluation-leakage failure the dataset methodology was designed to avoid, reappearing one layer up.

Corpus v2 removes trigger vocabulary entirely. No case states "replace", "add", "update", or any scope keyword:

- **Scope** is inferred from *which invoices are referenced* — an unpaid October invoice plus an ongoing retainer implies `OUTSTANDING_AND_FUTURE`.
- **Add vs replace** is inferred from *whether the existing account keeps a role* — this matters because RazorpayX permits [multiple fund accounts per contact](https://razorpay.com/docs/x/fund-accounts/), so treating every new account as a replacement is a real logic error.
- **Controls** contain account numbers, IFSC codes and change vocabulary while requesting no change to *this* vendor's destination (a third party's bank change, an internal process change, a change that already happened).

The baseline code is byte-identical across both runs. It was not weakened for v2.

**Stated honestly:** 14 hand-authored cases, not a large sample. The single miss (B3) was a case where our own ground truth was ambiguous and the model's stated reasoning was defensible.

Reproduce: `python eval/ablation.py`

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
data/                   generator, vendor master, 800 labeled cases
```

Quick start:

```
pip install -r requirements.txt
$env:GROQ_API_KEY="gsk_..."
python src/pipeline.py      # hero case
python eval/ablation.py     # ablation
```

---

## Evaluation methodology

Ground truth is assigned from independently authored scenario narratives, **not** derived from detector logic. Feature values are generated *from* each narrative afterwards, never the reverse.

- 120 synthetic vendors, 800 cases, stratified 70/30 dev/holdout
- Four narratives: `fraud_easy`, `fraud_hard`, `legit_easy`, `legit_hard`
- `fraud_hard` carries `name_match_score` 85–100 — it passes every bank-level check
- `legit_hard` has a genuinely new account plus genuine urgency — the false-positive canary
- The holdout is opened exactly once, at the end, and reported with its size

**Known limitation, stated up front:** the narratives and the rules were authored by the same team, so they share one mental model of fraud. A blind spot would appear in both the exam and the student. The holdout measures generalization across cases, not across threat models.

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
