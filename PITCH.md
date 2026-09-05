# BaseDrift

### A pre-authorization decision layer for outbound payments.

**[← README](README.md)** · **[Pitch](PITCH.md)** · **[Rulebook](RULEBOOK.md)** · **[Architecture](ARCHITECTURE.md)** · **[Evaluation](EVALUATION.md)** · **[What broke](FINDINGS.md)** · **[Build log](BUILD-LOG.md)** · **[Compliance](COMPLIANCE.md)**

---

---

## In one sentence

When a supplier emails asking you to pay a different bank account, **every
control in the payments stack can pass and the money can still be gone** —
because those controls verify the *account*, and nobody verifies the
*authorization*. BaseDrift is the layer that does, and it runs while the payout
is still frozen.

---

## The problem

Business Email Compromise is not a technically sophisticated attack. Somebody
gets into a supplier's mailbox, or registers a domain one character away from
it, and sends an ordinary-looking message: *our bank has changed, please update
our details before the next run.*

The finance clerk does exactly what a careful person does. They check the
account is real. They check the account holder's name matches. Both checks
pass — and both were always going to pass, because the attacker owns the
account they are asking you to pay.

**This is the part worth being precise about**, because it is where the whole
project comes from. Razorpay's Fund Account Validation compares the name the
bank returns against *"the name provided by the customer"* — a field supplied
by whoever creates the fund account. A finance team acting on a spoofed email
supplies the attacker's preferred name into the check themselves, and the check
returns a near-perfect match. It is not broken. It is answering a different
question.

| Control | Proves | Leaves open |
|---|---|---|
| Fund Account Validation | The account is real; the name matches what *you submitted* | Whether you submitted the right name |
| Reverse Penny Drop | The account holder's identity, via their own payment | Whether they are your vendor, or authorized this change |
| Approval Workflow | An internal role approved the payout | Whether the external vendor authorized the change |
| **BaseDrift** | **Authorization provenance of the change request** | — |

A verified bank account and a verified account holder are not proof that a
beneficiary change was authorized. Nothing in the stack was asking that
question.

---

## What it does

BaseDrift intercepts at the `payout.pending` webhook — the moment RazorpayX
freezes a payout awaiting approval, before any money has moved. It takes the
out-of-band request that triggered the change, resolves the payout's *actual*
destination, checks both against the merchant's own vendor master, and returns
one of two outcomes: release, or hold for a named human action.

The name is the thesis. **The vendor master is the base** — where a supplier
has been paid, established over time, under the merchant's control. Fraud is
**drift** away from it: a destination with no history, no corroboration, and
nothing behind the request to move it but the request itself.

---

## The architecture decision that matters

Everything else follows from one choice:

> **The model produces evidence. A deterministic rule engine makes the money
> decision.**

The LLM reads unstructured human communication — email, invoice, message — and
converts it into structured semantic evidence: what is being asked, what action
it implies, what scope it covers, whether the sender is applying pressure. It
is called once, it returns JSON, and it holds no tools. There is no path from a
model output to an approve endpoint.

Then a rule table with no model in it decides.

**This is not caution for its own sake.** It is the only structure where a
prompt injection is survivable. If the model can be talked into the wrong
label, the worst it achieves is downgrading a rejection recommendation to a
plain hold — never a release. That property is tested, not asserted, and it was
not always true: R2 once returned `ALLOW` on the model's intent label alone,
before any identity check ran, which made one manipulated field a total bypass.

### Proving the model is doing real work

The obvious objection to "the LLM does semantic normalization" is that a
regex would do. So it was measured against one.

Fourteen adversarial cases, hand-written to be semantically obvious and
lexically misleading. No case states "replace", "add", "update", or any scope
keyword. Scope has to be inferred from *which invoices are referenced*. Add
versus replace has to be inferred from *whether the old account keeps a role* —
which matters, because RazorpayX permits multiple fund accounts per contact, so
treating every new account as a replacement is a real logic error.

| | Keyword / regex baseline | Semantic layer |
|---|---|---|
| Correct on all three fields | **0 / 14** | **14 / 14** |
| Control messages wrongly flagged | **4 / 4** | **0 / 4** |

Identical inputs, identical ground truth, byte-identical baseline code across
runs. The controls are the half that matters most: messages full of account
numbers and change vocabulary that request no change to *this* vendor's
destination — a third party's bank change, an internal process change, a change
that already happened. The keyword baseline flags all four. The semantic layer
flags none.

**And an earlier version of this same measurement was wrong, which is recorded
rather than deleted.** Corpus v1 scored the keyword baseline at 92.3% — because
the paraphrases were written first and the trigger lists were written afterward
to match them. That is evaluation leakage, the exact failure the dataset
methodology was built to avoid, reappearing one layer up. The corpus was
rebuilt, and a guard now makes leakage a hard failure that stops rendering.

---

## What makes it a control rather than a detector

A detector produces a score. A control changes what people are able to do.

**Nothing rejects unattended.** The harshest outcome the engine can reach on
its own is a hold. Rules that once rejected now attach
`recommended_action="reject"` and wait for a person. This costs nothing in
capture — a hold and a rejection both leave the money where it is — and it
removed the only customer-facing failure the system had. **A hold costs a phone
call. A rejection costs a vendor their payment.**

**The two-person rule is enforced on the POST.** Whoever records a verification
cannot release the payment. Not a greyed-out button — a server-side refusal in
`casefile.may_release()`, because a control that lives only in the UI is not a
control.

**Evidence an attacker can author cannot say yes.** Mailbox-derived signals can
hold a payout and can never release one, enforced structurally: every inbox
signal is `WARN` or `INCONCLUSIVE`, and the engine raises rather than proceeding
if one ever returns `PASS`. Somebody inside a mailbox can manufacture months of
correspondence; *"this sender has written to us fifty times"* is a sentence an
attacker can make true.

**The safe state is inaction.** Bad signature, unknown vendor, unreadable
document, crashed process, BaseDrift entirely down — every failure leaves the
money exactly where it is. There is no code path that releases a payout on
error.

**A hold names what would resolve it.** Not "verify the vendor" — a specific
account the ₹1 must come from, chosen because it has settled payouts and was
not added by the channel now being verified. *"Prove you control an account on
file"* lets an attacker use one they planted.

---

## What broke

The findings are the part of this project worth reading, because a control that
hides its own near-misses is the failure mode it exists to prevent.

**The model could produce a release by itself.** R2 returned `ALLOW` the moment
the extractor reported "nothing is changing", before any identity check
constructed. Measured against the old engine: an unseen destination, the bank
reporting the account inactive, a name match of 3/100, an attacker-controlled
domain and urgency language returned `ALLOW`. R2 now verifies the claim against
where the money is actually going.

**A rejection rule corroborated itself.** R4 required impersonation evidence
*plus* an independent contextual warning — but the only signal that sets the
impersonation flag is the sender domain, which is itself a contextual warning,
so the second clause was satisfied by the first and constrained nothing. The
audit record was the real casualty: it told operators a rejection was
*"corroborated by sender_domain"*, counting the impersonation among the things
corroborating it. An operator reads that sentence before recommending somebody's
payment be refused.

**"On file" was treated as "established".** An attacker who gets an account onto
the vendor master with one accepted email can wait, then ask for the money to go
there — and every identity check passes *honestly*, on a fact that is itself the
fraud. When that scenario was added, **19 of 35 such cases released and recall
fell to 93.8%.** The engine now distinguishes an account confirmed by something
outside email from one that merely exists in a row.

**A corpus bug hid behind 216 passing tests.** A variable-name collision built
one declared corporate group instead of twenty, so every group-sharing case was
drawn from a single configuration. Every test passed, both evals ran clean, the
generator was byte-identical across runs, and the leakage guard reported zero.
Nothing asserted the corpus contained *enough* of a scenario to measure it. The
fix was a diversity floor on the dataset, which fails on the broken corpus where
every previous assertion passed.

**And the tests were checked for whether they bite.** `tools/mutate.py` breaks
fifteen stated invariants in turn — the two-person rule, tier separation, the
trust-store anchor, the server-side release guard — and asserts the suite
notices each one. **15 of 15 killed.** An earlier pass found two survivors, and
they were the finding: a compound guard whose two clauses could each be deleted
with all tests still green, because every test tripped both halves at once.

---

## What the numbers say

Scored once on a held-out split of 276 cases, never tuned against:

| | BaseDrift | hold everything, run no rules |
|---|---|---|
| fraud not released | **100%** | 100% |
| precision | **87.4%** | 86.3% |
| **legitimate payments rejected** | **0.0%** | 0.0% |
| legitimate payments held for review | 14.6% | 16.1% |
| released with no phone call | **20.7%** | 0% |

### And what they do not

**Recall of 100% is a ceiling, not a result.** It says the corpus cannot fail,
not that the system cannot. A synthetic corpus authored by the same person who
wrote the rules shares its blind spots — a fraud pattern neither the scenarios
nor the rules imagine appears in neither.

**On accuracy, the rule table is barely distinguishable from holding every
payout and phoning every vendor.** Three points of precision. That is not a
weakness being hidden; it is the honest reading, and it is why the interesting
number is elsewhere.

**The real number is volume.** At 20,000 payouts a day with 0.2% carrying a
change request, both systems catch the same fraud — but one holds **9 payouts a
day** and the other holds 20,000.

That 9 rests on a routine-hold rate measured at zero events, so it is the
optimistic end of a wide band. Assume a pessimistic 1% instead and it becomes
**208 a day**. The conclusion survives either way, which is the only reason it
is worth stating. On a corpus that is half fraud the two systems look three
points apart; on traffic anyone actually runs, one is a staffed desk and the
other is impossible.

**And 208 calls a day is still a team.** The argument for this system is not
that a human cannot keep up. It is that **a human does not know which call to
make.** BEC works precisely because the message looks routine — the clerk skips
the check because nothing seemed wrong, not because they were busy. *"Our
quarter closes Friday"* is written to make a person skip the check. A rule
engine does not feel deadlines.

---

## What is real, and what is not

Stated plainly, because a fraud control that overstates its own deployment
status is exactly the failure mode it exists to prevent.

**Real, and runs:** the decision engine and its rule table; the semantic layer
against a live model; the webhook handler including HMAC verification, replay
and idempotency handling; document correlation; the inbox triage funnel and its
MCP tool layer; the investigation agent; the operator dashboard; the case file
and its server-side two-person rule; 303 tests across 9 suites.

**Simulated:** every RazorpayX boundary. Fund-account and vendor lookups stand
in for API reads. Validation results are replayed schema-faithfully.
`razorpay_actions()` returns action *plans* — **nothing here calls Razorpay.**

**And that is not a shortcut.** RazorpayX's own documentation states that
Approval Workflow is unavailable in test mode, which means the `pending` state
does not exist there — so the event this system is built around cannot fire in
a sandbox, and the approve/reject endpoints have nothing to act on. Both halves
of the loop are blocked. Reaching the control point requires live mode on a real
current account with Payout Approval API access, which is a commercial
arrangement rather than an engineering task.

The honest next rung is shadow mode against a willing merchant: deciding
nothing, logging what it would have done. That is also the only thing that
produces a real false-positive rate, which is the number that actually decides
whether this is deployable.

---

## Why it is shaped this way

Three convictions, each of which cost something to hold:

**Measure it or delete it.** A component that survives only by being unmeasured
is the defect this project kept finding — in a signal that turned out to measure
relationship age rather than fraud, in a corpus that could not express the thing
being scored, in a guard whose second clause did nothing. Two signals were
retired rather than re-tuned into something unmeasurable.

**Report the number that hurts.** The null baseline is in every table, because
a pipeline that holds everything scores 100% on this data and accuracy alone
cannot tell the two apart. The holdout is reported with its size, whatever it
says.

**Prefer the recoverable failure.** A vendor who genuinely closed their old
account fails the penny drop through no fault of their own, and lands beside the
attacker in a state the evidence cannot separate. Both are held for a human
rather than released. That is not a gap — it is the design, and the reason is
that **a hold is recoverable and a release is not.**

---

**[Architecture](ARCHITECTURE.md)** · **[Rulebook](RULEBOOK.md)** · **[Evaluation](EVALUATION.md)** · **[What broke](FINDINGS.md)** · **[Build log](BUILD-LOG.md)** · **[Compliance](COMPLIANCE.md)**
