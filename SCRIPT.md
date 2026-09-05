# BaseDrift — 5-minute video script

**Spoken content: ~630 words → 4:12 at a natural pace.** The remaining ~35
seconds are clicking and silence during the demo, which lands the whole thing
at **4:47**.

That headroom is deliberate. Every recording runs long, and a script that fills
all five minutes on paper overruns in the room.

> **The word count is the constraint.** If you add a sentence, cut a sentence.
> `python tools/script_time.py` re-counts it if you want to check.

---

## Before you hit record

**Terminal 1** — dashboard running and already warm:

```bash
python src/demo.py --serve
```

Wait for `Inbox loaded` before recording. First load does real work.

**Browser tabs, pre-loaded, in this order:**

1. `http://localhost:8000/inbox`
2. `http://localhost:8000/case/pout_bec`
3. `PITCH.md` on GitHub — for the ablation table

**Terminal 2**, sitting unrun on:

```bash
python tools/mutate.py --list
```

**Zoom to 125%+.** Dashboard text is small and compression eats it.

**Read numbers off the screen, not off this page.** The inbox header says
`501 messages · 130 need review · 3 waiting on you`. That is a 500-message
slice (`INBOX_MESSAGES` in `src/demo.py`) of a 25,584-message corpus. If you
change the constant, change the line.

---

## 0:00 — 0:30 · The problem

> **SCREEN:** you, or a title card.

Somebody gets into your supplier's mailbox and sends an ordinary message: *our
bank details have changed, please update them before the next run.*

Your finance clerk does what a careful person does. Checks the account is real.
Checks the name matches. Both pass.

Both were always going to pass — the attacker owns the account they're asking
you to pay.

---

## 0:30 — 0:56 · Why the existing controls don't close it

> **SCREEN:** the control comparison table.

Razorpay's Fund Account Validation compares the name the bank returns against —
their words — *"the name provided by the customer."*

That name comes from whoever creates the fund account. A team acting on a
spoofed email feeds the attacker's own name into the check, and it comes back a
match.

The check isn't broken — it's answering a different question. Every control
here verifies the **account**. Nothing verifies the **authorization**.

---

## 0:56 — 1:18 · What I built

> **SCREEN:** the architecture diagram.

BaseDrift intercepts at the `payout.pending` webhook — the moment RazorpayX
freezes a payout for approval, before money moves.

It reads the request, resolves where the payout is *actually* going, and checks
both against the merchant's own vendor master. The master is the **base**.
Fraud is **drift** away from it.

---

## 1:18 — 2:12 · The decision that shapes everything

> **SCREEN:** diagram, then the ablation table.

One choice drives the rest: **the model produces evidence, a deterministic rule
engine makes the money decision.**

The LLM reads unstructured email and returns structured semantics. Called once,
returns JSON, holds no tools. There is no path from a model output to an
approve endpoint.

That's what makes prompt injection survivable: the worst a wrong label gets you
is a hold. Never a release.

> **SCREEN:** the 0/14 vs 14/14 table. *Pause here.*

And the model earns its place — I measured it against a regex baseline.
Fourteen cases where no message contains the trigger words.

Baseline, **zero out of fourteen**. Semantic layer, **fourteen out of
fourteen**. On the controls — full of account numbers, requesting no change —
the baseline flags all four. The model flags none.

---

## 2:12 — 3:12 · The demo

> **SCREEN:** live dashboard. Slow down.

The operator's view. Five hundred messages, a hundred and thirty needing
review — triage matched every sender against the supplier records with no model
call at all.

> **CLICK:** a flagged message → its case.

A held payout, with every signal and where it came from.

> **POINT AT:** the verification panel.

And a hold isn't a dead end. It names what would release it — not "verify the
vendor", but **this specific account**, with forty-three settled payouts. "An
account on file" would let an attacker use one they planted.

> **DO THIS IN ORDER — skipping step 1 gives you the wrong refusal.**
> 1. *Acting as* **Priya Menon** → **Supplier confirmed the change**
> 2. Still **Priya Menon** → **Release the payment**

So I recorded the verification myself. Watch what happens when I release it.

> **SCREEN:** the refusal — *"You recorded the verification on this case, so
> you cannot also release it. A different person must."* **Pause.**

Refused server-side. Whoever verifies cannot release. Not a greyed-out button —
a refusal on the POST.

---

## 3:12 — 4:02 · What broke

> **SCREEN:** FINDINGS.md, then Terminal 2.

The findings are what I'd actually want reviewed.

**The model could release a payout by itself.** One rule returned ALLOW the
moment the extractor said "nothing is changing" — before any identity check
ran. Unseen account, bank reporting it inactive, name match of three. Still
ALLOW.

**A rejection rule corroborated itself.** It wanted impersonation evidence plus
an independent warning — but the only signal setting impersonation *is* one of
those warnings. The second condition was satisfied by the first.

> **RUN:** `python tools/mutate.py --list`

So I stopped trusting the suite and mutation-tested it. Fifteen deliberate
breaks of invariants this project claims out loud. All fifteen killed — an
earlier pass found two that weren't, which is the point.

---

## 4:02 — 4:35 · What the numbers say, and don't

> **SCREEN:** the results table.

Scored once on a held-out split: **100% of fraud held, zero legitimate payments
rejected.**

But the honest reading is the next line. A pipeline that holds *everything* and
phones every vendor also scores 100%. On accuracy I'm three points from doing
nothing clever.

The real difference is volume: a couple of hundred payouts held a day, against
twenty thousand.

And 100% recall is a **ceiling, not a result**. It means my corpus can't fail,
not that the system can't.

---

## 4:35 — 4:50 · Close

> **SCREEN:** back to you.

The argument isn't that a human can't keep up — it's that they don't know
**which call to make**.

*"Our quarter closes Friday"* is written to make a person skip the check. A
rule engine doesn't feel deadlines.

---

# Notes for the take

**Slow down twice:** the ablation numbers (1:55) and the two-person refusal
(3:02). Those are the moments a technical reviewer leans in. Full beat of
silence after each.

**Speed up once:** 0:30–0:56 on the existing controls. Densest, least visual
thing you say.

**If you overrun**, cut in this order:

1. "with every signal and where it came from" (2:30)
2. The `sender_domain` detail in the corroboration finding (3:35)
3. The whole "What broke" second finding — keep the first and the mutation
   testing

Do **not** cut the mutation testing or "ceiling, not a result". The first is
the strongest evidence of rigour; the second is the credibility of everything
before it.

**Say "held", never "blocked".** The engine cannot reject anything on its own,
and the wrong verb undercuts the design you're demonstrating.

**Don't apologise for what's simulated.** If it comes up, once: the control
point needs live mode with Approval Workflow, because the `pending` state does
not exist in test mode — the event cannot fire in a sandbox. Documented
platform constraint, cited in the README, not a shortcut.
