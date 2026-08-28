# Compliance posture

What a production deployment of PayeeProof would have to satisfy, what the
design already satisfies, and what it does not.

Written now rather than later because the answer shapes the architecture. It is
not a legal opinion — several items below need one.

---

## Why the usual answer does not apply here

In analytics and classical ML you comply largely by **removing** personal data:
hash the vendor id, bucket the amount, train on de-identified features.

That option does not exist here. The semantic layer has to read

> *"Hi Meera, our treasury consolidated everything into a single facility.
> Everything should reach 351349409853, KKBK0238196 from here."*

to know a destination change is being requested at all. Strip the name, the
account and the phone and there is nothing left to reason about. **The personal
data is the input, not a feature derived from it.**

So compliance here comes from boundary control, provenance and retention — not
from anonymisation. Every item below follows from that.

---

## What applies

| Regime | Why it reaches this system |
|---|---|
| **RBI, Storage of Payment System Data (2018)** | Payment system data is subject to India storage requirements. The system sends payment data to a model. |
| **DPDP Act, 2023** | Vendor contacts, phone numbers, email addresses and — for proprietorships and partnerships — bank details are personal data. The merchant is the Data Fiduciary; PayeeProof is a Processor acting on its instructions. |
| **RBI Master Direction, Digital Payment Security Controls (2021)** | Injection defence, audit logging, access control, incident response. |
| **RBI, Outsourcing of IT Services (2023)** | A hosted model API is IT outsourcing: due diligence, contractual right to audit, concentration risk, a documented exit plan. |
| **MeitY AI advisories** | Non-binding, but directional on labelling and testing of deployed models. |
| **GDPR / EU AI Act** | Only with an EU nexus — but note a BLOCK is an automated decision producing a significant effect on the vendor, which is the category that attracts human-review obligations. |

---

## The largest open question: the model call leaves the country

Extraction currently runs against a US-hosted API. Every call carries payment
data and vendor personal data across a border.

Whether that is permissible turns on RBI's payment-data storage rules and the
DPDP transfer regime. **This is a legal question and must be answered before any
production use.** It is not an engineering preference.

**The architecture already contains the mitigation.** The pinned model,
`openai/gpt-oss-120b`, is **open-weight** — it can be self-hosted, in-country, on
infrastructure the merchant or its provider controls. A closed API-only model
would have removed that option entirely. `src/llm_client.py` is the single
place that talks to a provider, so redirecting inference to a self-hosted
endpoint is a change in one file rather than an architectural rewrite.

That was not originally a compliance decision. It is one now.

---

## What the design already satisfies

These are not incidental — they are the questions a regulator or an auditor
actually asks.

**The decision is reproducible even though the model is not.**
The extractor is non-deterministic: six runs of one identical email produced
three different outputs. But the model only produces *evidence*; a deterministic
rule engine makes the decision. The audit record stores the evidence that was
actually used, so **the same decision can be reconstructed from the record even
though re-running the model might not reproduce the evidence.** That is the
substantive answer to "how do you audit an AI decision".

**Every decision is attributable.**
`GET /case/{payout_id}` shows each signal, its result
(`PASS` / `WARN` / `INCONCLUSIVE` / `FAIL`), the finding, and where the evidence
came from. A held or rejected payout can be explained to the vendor whose money
it is. That is a contestability mechanism, not a convenience.

**Provenance is recorded.**
Each extraction carries `model_used` and `prompt_hash`. Model selection
auto-detects from a preference list, so without this the audit could not say
which model read the document — and *"an AI decided"* is not an auditable
statement. Two readings months apart are not comparable if the prompt changed
between them.

**Identity never comes from the model.**
Account continuity, GSTIN and domain resolve against the vendor master and the
payout's own fund account. A model error cannot produce a wrong rejection on
identity grounds.

**The irreversible action requires a human.**
BLOCK emits the payout rejection *and* a fund-account deactivation flagged
`requires_human_confirmation`. Rejecting one payout is recoverable; disabling a
vendor's destination is not, and at the measured false-block rate that would be
roughly one legitimate vendor in 170 losing a destination on a decision nobody
reviewed.

**The safe state is inaction.**
A pending payout stays pending unless something explicitly approves it. No error
path releases money.

**Injection defence exists and is tested.**
`sanitize()` filters by Unicode category rather than an enumerated character
list, covering bidirectional overrides and the Unicode Tag block — the two most
effective ways to hide instructions in a document.

---

## What production would still require

Ordered by how much work they are, not how important.

**1. Model and prompt pinning, not auto-detection.**
`MODEL_PREFERENCE` selects whatever is live. Provenance is now recorded, but a
regulated decision should be made by a *known* model, changed deliberately. A
retired model has already caused a silent change once during development.

**2. Retention and erasure.**
`raw_llm_output` keeps 1000 characters of the model's parse; the document store
keeps full email bodies; the audit trail keeps both. All indefinitely, in memory,
unencrypted. DPDP requires storage limitation and erasure on request, and the
audit trail is simultaneously a compliance *requirement* and a personal-data
*liability*. Those two pull in opposite directions and the retention period has
to be a stated decision, not a default.

**3. Access control.**
`POST /documents` accepts a document for any vendor from anyone who can reach it.
The dashboard shows account numbers and vendor identity to anyone who can load
it. The webhook's HMAC authenticates Razorpay — it does nothing for these two.

**4. Encryption at rest and in transit for the stores.**
Vendor master, document store and audit trail.

**5. Data localisation.** See above.

**6. Fiduciary obligations.**
Breach notification path, a grievance mechanism, and a named contact — required
of a Data Fiduciary under DPDP, and the merchant will need PayeeProof to support
them contractually.

**7. Outsourcing controls for the model provider.**
Due diligence, right to audit, and an exit plan. Model deprecation is a live
availability risk — `llama-3.3-70b-versatile` was retired during development and
`MODEL_PREFERENCE` still carries a note saying so.

**8. Purpose limitation on inbox access.**
The planned MCP integration reads the merchant's own AP inbox. Consent and
purpose have to be explicit and narrow: fraud control on payout destinations, not
general correspondence analysis. Inbox-derived signals are also constrained
technically — Tier 2 only, corroborating and never identity-establishing —
because an inbox is attacker-controllable at scale.

---

## Standing constraints

Two that follow directly from the above and should not be traded away:

- **Whatever reads the document must not be able to act on it.** The model
  produces evidence; the rule engine decides; the irreversible action needs a
  human. Collapsing any of those three removes the basis on which the system is
  auditable at all.

- **Anything that widens what the model sees widens the compliance surface.**
  Inbox access, multi-document correlation and longer context each increase the
  personal data crossing the inference boundary. That is a reason to keep the
  boundary in one file, not a reason to avoid the features.
