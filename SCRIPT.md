# BaseDrift — 5-minute video script

**Target 4:50.** Ten seconds of headroom, because every recording runs long and
the last thing you want is to be rushing the close.

Roughly 660 spoken words. If you read at a natural pace — not fast — that lands
around 4:30, leaving room for the demo to breathe.

---

## Before you hit record

**Terminal 1** — the dashboard, already running and already warm:

```bash
python src/demo.py --serve
```

Wait for `Inbox loaded` before recording. First load does real work and you do
not want that on tape.

**Browser tabs, in this order, all pre-loaded:**

1. `http://localhost:8000/inbox` — the mailbox
2. `http://localhost:8000/case/pout_bec` — the fraud case, evidence visible
3. `README.md` on GitHub — for the ablation table

**Terminal 2** — sitting on this command, unrun:

```bash
python tools/mutate.py --list
```

**Check before you start:** browser zoom at 125% or more. Dashboard text is
small and the compression will eat it.

**Read the numbers off the screen, not off this page.** The inbox header shows
`501 messages · 130 need review · 3 waiting on you`, and the script quotes those
three figures so your voice matches the pixels. They come from a 500-message
slice (`INBOX_MESSAGES` in `src/demo.py`) of a 25,584-message corpus — if you
change that constant, change the line.

---

## 0:00 — 0:30 · The problem

> **SCREEN:** You, or a plain title card. No dashboard yet.

Somebody gets into your supplier's mailbox. They send an ordinary message —
*our bank details have changed, please update them before the next run.*

Your finance clerk does what a careful person does. They check the account is
real. They check the account holder's name matches. Both checks pass.

And both were always going to pass — because the attacker owns the account
they're asking you to pay.

That's Business Email Compromise. It isn't sophisticated. It works because
every control in the payments stack verifies the **account**, and nobody
verifies the **authorization**.

---

## 0:30 — 1:00 · Why the existing controls don't close it

> **SCREEN:** The comparison table from the README.

This is worth being precise about. Razorpay's Fund Account Validation compares
the name the bank returns against — their words — *"the name provided by the
customer."*

That name comes from whoever creates the fund account. So a finance team acting
on a spoofed email supplies the attacker's preferred name into the check
themselves. It comes back a near-perfect match.

The check isn't broken. It's answering a different question. Reverse Penny Drop
has the same shape — it proves somebody controls the account, and the attacker
does.

---

## 1:00 — 1:25 · What I built

> **SCREEN:** The architecture diagram.

BaseDrift is a pre-authorization decision layer. It intercepts at the
`payout.pending` webhook — the moment RazorpayX freezes a payout for approval,
before any money moves.

It reads the request that asked for the change, resolves where the payout is
*actually* going, and checks both against the merchant's own vendor master.

The vendor master is the **base** — where this supplier has always been paid.
Fraud is **drift** away from it.

---

## 1:25 — 2:15 · The decision that shapes everything

> **SCREEN:** Diagram, then the ablation table.

One architectural choice drives the rest: **the model produces evidence, and a
deterministic rule engine makes the money decision.**

The LLM reads unstructured email and returns structured semantics — what's being
asked, what action it implies, what scope it covers. It's called once, it
returns JSON, and it holds no tools. There is no path from a model output to an
approve endpoint.

That's what makes prompt injection survivable. If you talk the model into the
wrong label, the worst you achieve is downgrading a rejection to a hold. Never a
release.

> **SCREEN:** the 0/14 vs 14/14 table.

And the model is doing real work — I measured it against a regex baseline.
Fourteen adversarial cases where no message contains the trigger words. Scope
has to be inferred from *which invoices are referenced*; add-versus-replace from
*whether the old account keeps a role*.

Keyword baseline: **zero out of fourteen.** Semantic layer: **fourteen out of
fourteen.** And on the control messages — full of account numbers but requesting
no change — the baseline flags all four. The model flags none.

---

## 2:15 — 3:20 · The demo

> **SCREEN:** Live dashboard. Slow down here.

This is the operator's view — the mailbox as triage saw it. Five hundred and
one messages, a hundred and thirty of them needing review, three waiting on a
person right now. Triage matched every sender against the supplier records with
no model call at all; only what survived that gets read in full.

> **CLICK:** a flagged message → its case.

Here's a held payout. Every signal, what it found, and where it came from —
the bank, our own records, or the email. The email comes first on the page and
the machine's reading second, deliberately: an operator who reads the verdict
first inherits its conclusion.

> **POINT AT:** the verification panel.

And a hold isn't a dead end. It names what would release it — not "verify the
vendor," but **this specific account**, chosen because it has forty-three
settled payouts and was verified at onboarding. "Prove you control an account
on file" would let an attacker use one they planted.

> **DO THIS, in order — it does not work if you skip the first step.**
> The case starts with nothing recorded, so releasing straight away gets you
> *"Nothing is verified yet"*, which is the wrong refusal for this point.
>
> 1. **Acting as → Priya Menon**, then click **Supplier confirmed the change**.
> 2. Leave *Acting as* on **Priya Menon** and click **Release the payment**.

So I've just recorded the verification myself. Now watch what happens when I
try to release the same payment.

> **SCREEN:** the refusal. It reads, verbatim:
> *"You recorded the verification on this case, so you cannot also release it.
> A different person must."*

Refused, server-side. Whoever verifies cannot release. That's not a greyed-out
button, it's a refusal on the POST — a control that only lives in the UI isn't a
control.

> **OPTIONAL, if you have the time:** switch *Acting as* to **Rahul Iyer** and
> release. It goes through. That contrast is what makes the point land — it is a
> segregation rule, not a broken button.

---

## 3:20 — 4:10 · What broke

> **SCREEN:** FINDINGS.md, then Terminal 2.

The findings are the part I'd actually want reviewed.

**The model could release a payout by itself.** One rule returned ALLOW the
moment the extractor said "nothing is changing" — before any identity check ran.
An unseen account, the bank reporting it inactive, a name match of three out of
a hundred, still returned ALLOW.

**A rejection rule corroborated itself.** It required impersonation evidence
plus an independent warning — but the only signal that sets impersonation *is*
one of those warnings. The second condition was satisfied by the first and
constrained nothing.

**And a corpus bug hid behind two hundred and sixteen passing tests.** A
variable collision built one corporate group instead of twenty. Every test
passed. Nothing asserted the dataset contained enough of a scenario to measure
it.

> **RUN:** `python tools/mutate.py --list`

So I stopped trusting the suite and mutation-tested it. Fifteen deliberate
breaks of invariants this project claims out loud. All fifteen killed — and an
earlier pass found two that weren't, which is exactly the point.

---

## 4:10 — 4:45 · What the numbers say, and don't

> **SCREEN:** the results table.

Scored once on a held-out split, never tuned against: **100% of fraud held,
zero legitimate payments rejected.**

But the honest reading is the next line. A pipeline that holds *everything* and
phones every vendor also scores 100%. On accuracy I'm three points from doing
nothing clever at all.

**The real number is volume.** At twenty thousand payouts a day, both systems
catch the same fraud — but one holds a couple of hundred payouts and the other
holds twenty thousand.

And recall of 100% is a **ceiling, not a result.** It means my corpus can't
fail, not that the system can't.

---

## 4:45 — 4:55 · Close

> **SCREEN:** back to you, or the title card.

The argument isn't that a human can't keep up. It's that a human doesn't know
**which call to make** — BEC works because the message looks routine.

The payout is held by default, and something has to actively release it.
*"Our quarter closes Friday"* is written to make a person skip the check.

A rule engine doesn't feel deadlines.

---

# Notes for the take

**Where to slow down:** the ablation numbers (1:55) and the two-person refusal
(3:05). Those are the two moments a technical reviewer leans in. Give each a
full beat of silence afterward.

**Where to speed up:** the 0:30–1:00 stretch on existing controls. It's the
densest and least visual thing you say. Keep it moving.

**If you overrun**, cut in this order:

1. The optional "release as Rahul" contrast at the end of the demo.
2. The corpus-bug finding at 3:55 — the other two findings are stronger.
3. The Reverse Penny Drop sentence at 0:55.
4. The "email comes first on the page" aside at 2:45.

Do **not** cut the mutation testing or the "ceiling, not a result" line. The
first is the strongest evidence of rigour and the second is the credibility of
everything before it.

**Say "held", never "blocked".** The system cannot reject anything on its own,
and using the wrong verb undercuts the design decision you're demonstrating.

**Don't apologise for what's simulated.** State it once if it comes up: the
control point requires live mode with Approval Workflow enabled, because the
`pending` state does not exist in test mode — the event cannot fire in a
sandbox. That's a documented platform constraint, not a shortcut. It's in the
README with the citation.
