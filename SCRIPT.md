# BaseDrift — recording guide

**Everything: what is on screen, what you click, where you point, what you say.**

Spoken content is ~637 words → **4:15** at an unhurried pace. Clicking and two
deliberate pauses add ~35s. **Total 4:50** against a five-minute limit.

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

## 0:00 — 0:28 · The problem

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

## 0:28 — 0:57 · Why the existing controls don't close it

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
> here verifies the account. Nothing verifies authorization.

**Delivery:** this is the densest, least visual stretch. Keep it moving.

---

## 0:57 — 1:18 · What I built

**ON SCREEN** Tab 5 — scroll up to the **architecture diagram**.

**POINT AT** the teal *Semantic layer* box, then the gold *Rule engine* diamond.

**SAY**

> BaseDrift intercepts at the payout-dot-pending webhook — the moment RazorpayX
> freezes a payout, before money moves. It checks where the payout is actually
> going against the merchant's own vendor master. The master is the base; fraud
> is drift away from it.

---

## 1:18 — 2:14 · The decision that shapes everything

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

> And the model is open-weight — moving inference in-country is a config
> change, not a rewrite. That's compliance, not preference.

**PAUSE** — one full beat while the ablation table is on screen.

> And the model earns its place. Fourteen adversarial cases, no trigger words
> in any of them.
>
> Baseline, zero out of fourteen. Semantic layer, fourteen out of fourteen. On
> the controls — full of account numbers, requesting no change — the baseline
> flags all four. The model flags none.

---

## 2:14 — 3:12 · The demo

### 2:14 · the mailbox

**ON SCREEN** Tab 1 — `/inbox`

**POINT AT** the header line: `501 messages · 130 need review · 3 waiting on you`

**SAY**

> The operator's view. Five hundred messages, a hundred and thirty needing
> review — triage matched every sender against the records with no model call.

### 2:22 · the message

**DO** Click the row **INV-4819 — accounts@novasystems.com**.

**POINT AT** these three sentences in the body, in order:

1. *"INV-4819 from October is still open on our ledger, and the retainer runs through March"*
2. *"Our quarter cuts off on Friday"*
3. *"Replying here rather than by phone"*

**SAY**

> Which invoices are covered is never stated, only implied. There's the
> deadline. There's the request to keep it off the phone. No trigger words
> anywhere.

### 2:42 · the decision queue

**DO** Click **Open the decision →**, then **← All decisions**.

**POINT AT** the header: `133 decisions · 5 not released · 1 recommended for rejection`,
then the **pout_mule** row tagged `RECOMMEND REJECT`.

**SAY** nothing — walk through it while the previous line lands. The header
does the work: five held, exactly one carrying a rejection recommendation.

### 2:52 · the control

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

## 3:12 — 3:48 · What broke

**ON SCREEN** Tab 5 — navigate to **FINDINGS.md**.

**SAY**

> The model could release a payout by itself. One rule returned ALLOW the
> moment the extractor said "nothing is changing" — before any identity check
> ran at all.

**DO** Switch to terminal 2. Run `python tools/mutate.py --list`.

**POINT AT** the scrolling list of invariants.

**SAY**

> So I stopped trusting the suite and mutation-tested it. Fifteen deliberate
> breaks of invariants this project claims out loud. All fifteen killed — an
> earlier pass found two that weren't, which is the point.

---

## 3:48 — 4:40 · What the numbers say, and don't

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
> The real difference is volume: a couple of hundred held a day, against
> twenty thousand.
>
> And a hundred percent recall is a ceiling, not a result. It means my corpus
> can't fail, not that the system can't.
>
> Nothing here calls Razorpay, either. The pending state doesn't exist in test
> mode, so the event this is built around can't fire in a sandbox. And it
> protects vendors you've already onboarded — onboarding fraud is a different
> problem.

**Delivery:** do not rush this. Volunteering the baseline, the ceiling and the
missing integration in one breath is the most credible thirty seconds in the
video. A reviewer who finds any of the three for themselves afterwards
discounts everything else you said.

---

## 4:40 — 4:57 · Close

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

1. The decision-queue navigation at 2:42 — go straight from the message to
   the case
2. *"Unseen account, bank reporting it inactive, name match of three"* at 3:14
   — the finding survives without the detail
3. The scope sentence at 4:32 (*"onboarding fraud is a different problem"*) —
   the Razorpay sentence before it matters more

Never cut the mutation testing, *"ceiling, not a result"*, or *"nothing here
calls Razorpay"*. The first is the strongest evidence of rigour; the other two
are the credibility of everything else you said.

---

# Three rules for the whole take

**Say "held", never "blocked".** The engine cannot reject anything on its own.
The wrong verb undercuts the design you are demonstrating.

**Don't claim the agent or the MCP layer.** Both exist -- `src/investigator.py`
and `mcp/inbox_server.py`, read-only and merchant-scoped -- and a rubric that
says "spend the most time on agents" makes them tempting. But this project's
own notes call the MCP layer a well-shaped seam with nothing plugged into it:
no client, no transport, and the agent calls its tools in a fixed order rather
than choosing them. Six seconds of mentioning it buys a question you cannot
answer well, in a video whose whole argument is that this system does not
overstate itself. The honest AI-native story is the semantic layer and its
containment, and that already has the largest share of the running time. If
asked, say exactly the above.

**Quote the holdout, never the dev split.** 87.4% is the honest number. 89.3%
is dev, which the rules were built on, and quoting it is the one thing a
reviewer will catch.

**Don't apologise for what's simulated.** Once, if it comes up: the control
point needs live mode with Approval Workflow, because the `pending` state does
not exist in test mode — the event cannot fire in a sandbox. Documented
platform constraint, cited in the README, not a shortcut.
