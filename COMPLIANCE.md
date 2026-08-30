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

**And the chain is now two links, not one.** Inference reaches the model through
a ROUTING layer: `PAYEEPROOF_BASE_URL` points at OpenRouter, which dispatches to
whichever host serves the model — DeepInfra in the runs behind the reported
figures. Under DPDP each processor in that chain has to be named and
contracted, so this is two due-diligence exercises rather than one.

Worse without care: routing is dynamic. The first live call through it was
served by a host nobody had assessed. Two mitigations exist and both are
deliberate rather than incidental — `PAYEEPROOF_PROVIDER` pins the host with
`allow_fallbacks=False`, and `ExtractionResult.served_by` records who actually
ran each call, so the audit trail names the processor rather than only the
model. **Pinning is opt-in.** Unpinned, the processor for a given decision is
whatever the router chose that second, which is not a defensible position for
regulated data.

Whether that is permissible turns on RBI's payment-data storage rules and the
DPDP transfer regime. **This is a legal question and must be answered before any
production use.** It is not an engineering preference.

**The architecture already contains the mitigation.** The pinned model,
`openai/gpt-oss-120b`, is **open-weight** — it can be self-hosted, in-country, on
infrastructure the merchant or its provider controls. A closed API-only model
would have removed that option entirely. `src/llm_client.py` is the single
place the DECISION PATH talks to a provider, so redirecting inference to a
self-hosted endpoint is a change in one file rather than an architectural
rewrite.

That qualifier is load-bearing and was missing. `eval/ablation.py` is
deliberately standalone — it reproduces the ablation without importing the repo
— and it carried its own hardcoded provider URL, so the claim "the only module
that talks to a provider" was false as written. It now reads the same
`PAYEEPROOF_*` variables, which is what actually matters: an evaluation that can
silently measure a different provider than the system runs on is not evidence
about the system.

That was not originally a compliance decision. It is one now.

**And it is no longer only a claim.** The provider is now configuration rather
than code — `PAYEEPROOF_BASE_URL`, `PAYEEPROOF_API_KEY`, `PAYEEPROOF_MODEL` and
`PAYEEPROOF_PROVIDER` — and the switch has actually been exercised: the v2
extraction corpus was produced through a different provider than v1's, running
the same weights, with no change to the prompt, the rules, or any other file.
Pointing `PAYEEPROOF_BASE_URL` at a self-hosted endpoint inside India is the
same operation, and nothing in the codebase distinguishes the two cases.

Two consequences worth stating precisely:

- **Portability is a property of the model, not of good intentions.** Every
  closed, hosted-only alternative considered would have ended this option
  permanently, because a model nobody can run is a model nobody can run
  in-country. Choosing an open-weight model is what keeps the mitigation
  available, and it is the reason the provider change cost nothing.
- **The audit record now names the host, not just the model.** An open-weight
  model is served by many companies, and one routing layer moved between ~18 of
  them mid-evaluation. `ExtractionResult.served_by` records which company
  actually ran the model for each decision, because "gpt-oss-120b decided this
  payout" does not identify a data processor — and under DPDP, naming the
  processor is the point.

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

**1. Model and prompt pinning, not auto-detection.** *(half-closed)*
`MODEL_PREFERENCE` still selects whatever is live, and that remains the default.
`PAYEEPROOF_MODEL` now pins a model id and skips detection entirely, and every
decision records the model *and* the host that ran it — so the capability exists
and the provenance is real. What is outstanding is that a regulated deployment
should have pinning ON, not merely available. A retired model caused a silent
change once during development, which is the argument.

**2. Retention and erasure.** *(surface grew with the inbox work)*
`raw_llm_output` keeps 1000 characters of the model's parse; the document store
keeps full email bodies; the audit trail keeps both. The inbox layer added more:
`InboxServer` holds every message body it serves — 7,176 in the corpus
configuration — and documents now also carry the inbox signals and triage
metadata gathered at ingest. All indefinitely, in memory, unencrypted. DPDP requires storage limitation and erasure on request, and the
audit trail is simultaneously a compliance *requirement* and a personal-data
*liability*. Those two pull in opposite directions and the retention period has
to be a stated decision, not a default.

**3. Access control.** *(worse — a second unauthenticated endpoint)*
`POST /documents` accepts a document for any vendor from anyone who can reach it.
`POST /messages` now accepts a raw message the same way, and is the more
attractive target of the two: a message posted there is triaged, becomes a
document, and carries inbox signals into a real payout decision. The dashboard
shows account numbers and vendor identity to anyone who can load it. The
webhook's HMAC authenticates Razorpay — it does nothing for any of these three.

Related and not yet built: `INBOX_CURSOR.md` records why message dedupe must key
on the provider-assigned id rather than the `Message-ID:` header. The header is
attacker-written, so keying on it would let a sender collide with a processed id
and have their own change request silently dropped.

**4. Encryption at rest and in transit for the stores.**
Vendor master, document store and audit trail.

**5. Data localisation.** See above.

**6. Fiduciary obligations.**
Breach notification path, a grievance mechanism, and a named contact — required
of a Data Fiduciary under DPDP, and the merchant will need PayeeProof to support
them contractually.

**7. Outsourcing controls for the model provider.** *(now two providers)*
Due diligence, right to audit, and an exit plan — for the routing layer *and*
for whichever host it dispatches to. See the border section above: unpinned,
that host is not a fixed entity.

The exit plan is the one part in good shape. The model is open-weight and the
provider is five environment variables, so moving — including to a self-hosted
in-country endpoint — is configuration. That has been exercised: the v2 corpus
was produced through a different provider than v1's, with no other file changed.

Model deprecation remains a live availability risk. `llama-3.3-70b-versatile`
was retired during development and `MODEL_PREFERENCE` still carries a note
saying so.

**8. Purpose limitation on inbox access.** *(now live, not planned)*
This is no longer prospective. Triage is wired into the decision path: `POST
/messages` ingests a raw message, and the inbox signals gathered at that moment
are stored on the document and reach `decide()` when the payout goes pending.
The MCP tool layer reads the merchant's own AP inbox. Consent and
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
