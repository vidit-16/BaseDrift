# BaseDrift — recording guide

**Everything: what is on screen, what you click, where you point, what you say.**

Spoken content is ~640 words → **4:16** at an unhurried pace. Clicking and two
deliberate pauses add ~35s. **Total 4:51** against a five-minute limit.

```bash
python tools/script_time.py
```

Re-counts it. If you add a sentence, cut a sentence.

---

# Before you hit record

### 1 · Start the dashboard and let it warm up

```bash
python src/demo.py --serve
```

Wait for `Inbox loaded: 501 messages triaged.` **Do not restart it after this.**
A cold first load does real work and you do not want that on tape.

### 2 · Open exactly these tabs, in this order

| # | URL | what it is |
|---|---|---|
| 1 | `http://localhost:8000/inbox` | the mailbox |
| 2 | `http://localhost:8000/message/%3C162996995f988705%40vendor.mail%3E` | INV-4819, the message you read on camera |
| 3 | `http://localhost:8000/` | the decision queue |
| 4 | `http://localhost:8000/case/pout_0073` | the case, where the controls live |
| 5 | `https://github.com/vidit-16/BaseDrift` | the README, scrolled to **The ablation result** |

Tabs 2 and 4 are reachable by clicking from tabs 1 and 3 — open them anyway as
a fallback, so a mis-click never costs you a take.

### 3 · Second terminal, sitting on this command **unrun**

```bash
python tools/mutate.py --list
```

### 4 · Final checks

- Browser zoom **125% or more**. Dashboard text is small and compression eats it.
- Close every other tab. A visible unrelated tab is the thing people notice.
- The numbers you say are read **off the screen**. If the demo is reseeded and
  they change, change the words.

---

# The take

---

## 0:00 — 0:30 · The problem

**ON SCREEN** You, or a plain title card. No dashboard yet.

**SAY**

> Somebody gets into your supplier's mailbox and sends an ordinary message: our
> bank details have changed, please update them before the next run.
>
> Your finance clerk does what a careful person does. Checks the account is
> real. Checks the name matches. Both pass.
>
> Both were always going to pass — the attacker owns the account they're asking
> you to pay.

**Delivery:** land *"both were always going to pass"* slowly. It is the hook.

---

## 0:30 — 0:56 · Why the existing controls don't close it

**ON SCREEN** Tab 5 — GitHub README, the control comparison table.

**POINT AT** the **Fund Account Validation** row, then the **BaseDrift** row.

**SAY**

> Razorpay's Fund Account Validation compares the name the bank returns against
> — their words — "the name provided by the customer."
>
> That name comes from whoever creates the fund account. A team acting on a
> spoofed email feeds the attacker's own name into the check, and it matches.
>
> The check isn't broken — it's answering a different question. Every control
> here verifies the account. Nothing verifies the authorization.

**Delivery:** this is the densest, least visual stretch. Keep it moving.

---

## 0:56 — 1:18 · What I built

**ON SCREEN** Tab 5 — scroll up to the **architecture diagram**.

**POINT AT** the teal *Semantic layer* box, then the gold *Rule engine* diamond.

**SAY**

> BaseDrift intercepts at the payout-dot-pending webhook — the moment RazorpayX
> freezes a payout for approval, before money moves.
>
> It reads the request, resolves where the payout is actually going, and checks
> both against the merchant's own vendor master. The master is the base. Fraud
> is drift away from it.

---

## 1:18 — 2:12 · The decision that shapes everything

**ON SCREEN** Tab 5 — diagram, then scroll to **The ablation result** table.

**POINT AT** the arrow from *Rule engine* to *Release*, then the *0/14* and
*14/14* cells.

**SAY**

> One choice drives the rest: the model produces evidence, a deterministic rule
> engine makes the money decision.
>
> The LLM reads unstructured email and returns structured semantics. Called
> once, returns JSON, holds no tools — no path from a model output to an
> approve endpoint.
>
> That's what makes prompt injection survivable: the worst a wrong label gets
> you is a hold. Never a release.

**PAUSE** — one full beat while the ablation table is on screen.

> And the model earns its place. Fourteen adversarial cases, no trigger words
> in any of them.
>
> Baseline, zero out of fourteen. Semantic layer, fourteen out of fourteen. On
> the controls — full of account numbers, requesting no change — the baseline
> flags all four. The model flags none.

---

## 2:12 — 3:12 · The demo

### 2:12 · the mailbox

**ON SCREEN** Tab 1 — `/inbox`

**POINT AT** the header line: `501 messages · 130 need review · 3 waiting on you`

**SAY**

> The operator's view. Five hundred messages, a hundred and thirty needing
> review — triage matched every sender against the supplier records, no model
> call.

### 2:24 · the message

**DO** Click the row **INV-4819 — accounts@novasystems.com**.

**POINT AT** these three sentences in the body, in order:

1. *"INV-4819 from October is still open on our ledger, and the retainer runs through March"*
2. *"Our quarter cuts off on Friday"*
3. *"Replying here rather than by phone"*

**SAY**

> Which invoices are covered is never stated — it's implied by the two that
> are named. There's the deadline. And there's the request to keep it off the
> phone. No trigger words anywhere.

### 2:44 · the decision queue

**DO** Click **Open the decision →**, then **← All decisions**.

**POINT AT** the header: `133 decisions · 5 not released · 1 recommended for rejection`,
then the **pout_mule** row tagged `RECOMMEND REJECT`.

**SAY**

> Five held. Exactly one carries a rejection recommendation.

### 2:56 · the control

**DO** Click **pout_0073**. Scroll to **Verification — what would release this**.

**POINT AT** the account number `064987339232` and the line beneath it.

**SAY**

> A hold isn't a dead end. It names what would release it — not "verify the
> vendor", but this specific account, with thirty settled payouts. "An account
> on file" would let an attacker use one they planted.

**DO — in this order. Skipping step 1 gives you the wrong refusal.**

1. *Acting as* → **Priya Menon**, click **Supplier confirmed the change**
2. Leave it on **Priya Menon**, click **Release the payment**

**SAY**

> So I recorded the verification myself. Watch what happens when I release it.

**ON SCREEN** the refusal, verbatim: *"You recorded the verification on this
case, so you cannot also release it. A different person must."*

**PAUSE** — one full beat.

**SAY**

> Refused server-side. Whoever verifies cannot release. Not a greyed-out button
> — a refusal on the POST.

---

## 3:12 — 4:02 · What broke

**ON SCREEN** Tab 5 — navigate to **FINDINGS.md**.

**SAY**

> The model could release a payout by itself. One rule returned ALLOW the
> moment the extractor said "nothing is changing" — before any identity check
> ran. Unseen account, bank reporting it inactive, name match of three. Still
> ALLOW.
>
> A rejection rule corroborated itself — the only signal setting impersonation
> was also the warning meant to corroborate it. The second condition was
> satisfied by the first.

**DO** Switch to terminal 2. Run `python tools/mutate.py --list`.

**POINT AT** the scrolling list of invariants.

**SAY**

> So I stopped trusting the suite and mutation-tested it. Fifteen deliberate
> breaks of invariants this project claims out loud. All fifteen killed — an
> earlier pass found two that weren't, which is the point.

---

## 4:02 — 4:35 · What the numbers say, and don't

**ON SCREEN** Tab 5 — README, **At a glance** results table.

**POINT AT** the **second column** — `hold everything, run no rules`.

**SAY**

> Scored once on a held-out split: a hundred percent of fraud held, zero
> legitimate payments rejected.
>
> But the honest reading is the next column. A pipeline that holds everything
> and phones every vendor also scores a hundred percent. On accuracy I'm one
> point from doing nothing clever.
>
> The real difference is volume: a couple of hundred payouts held a day,
> against twenty thousand.
>
> And a hundred percent recall is a ceiling, not a result. It means my corpus
> can't fail, not that the system can't.

**Delivery:** do not rush this. Volunteering the baseline is the most
credible thing in the video.

---

## 4:35 — 4:50 · Close

**ON SCREEN** Back to you, or the title card.

**SAY**

> The argument isn't that a human can't keep up — it's that they don't know
> which call to make.
>
> "Our quarter closes Friday" is written to make a person skip the check. A
> rule engine doesn't feel deadlines.

---

# If it goes wrong

**The refusal doesn't appear.** You skipped step 1, or *Acting as* flipped to
someone else. The message you want names Priya Menon. If you see *"Nothing is
verified yet"*, that is the wrong refusal — record the callback first.

**A page 404s.** Tabs 2 and 4 are already open as fallbacks. Switch, don't
reload.

**You overrun.** Cut in this order:

1. The decision-queue beat at 2:44 — go straight from the message to the case
2. The `sender_domain` detail in the corroboration finding at 3:30
3. The second "what broke" finding entirely — keep the first and the mutations

Never cut the mutation testing or *"ceiling, not a result"*. The first is the
strongest evidence of rigour; the second is the credibility of everything
before it.

---

# Three rules for the whole take

**Say "held", never "blocked".** The engine cannot reject anything on its own.
The wrong verb undercuts the design you are demonstrating.

**Quote the holdout, never the dev split.** 87.4% is the honest number. 89.3%
is dev, which the rules were built on, and quoting it is the one thing a
reviewer will catch.

**Don't apologise for what's simulated.** Once, if it comes up: the control
point needs live mode with Approval Workflow, because the `pending` state does
not exist in test mode — the event cannot fire in a sandbox. Documented
platform constraint, cited in the README, not a shortcut.
