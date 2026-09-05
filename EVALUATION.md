# BaseDrift — Evaluation

How this system was measured, what the numbers mean, and — at greater length
than is comfortable — what they cannot tell you.

The short version: **recall of 100% is a ceiling, not a result.** It says the
corpus cannot fail, not that the system cannot. Everything below is an attempt
to be precise about which claims rest on the data and which do not.

**[← README](README.md)** · **[Findings](FINDINGS.md)** · **[Rulebook](RULEBOOK.md)** · **[Build log](BUILD-LOG.md)**

---

## Evaluation methodology

Ground truth is assigned from independently authored scenario narratives, **not** derived from detector logic. Feature values are generated *from* each narrative afterwards, never the reverse.

- 120 synthetic vendors, 900 cases, stratified 70/30 — 624 dev, 276 holdout
- Eighteen narratives: `fraud_easy`, `fraud_hard`, `fraud_compromised`,
  `fraud_mule`, `fraud_sim_swap`, `fraud_planted_account`,
  `fraud_first_contact`, `fraud_thread_hijack`,
  `fraud_exploit_planted_account`, `legit_easy`, `legit_hard`,
  `legit_rebrand`, `legit_add_account`, `legit_unreachable`,
  `legit_group_shared_account`, `legit_second_account`,
  `legit_added_then_paid`, `legit_switch_to_known_account`
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

| system | recall | precision | held | **false BLOCK** |
|---|---|---|---|---|
| null — hold everything, no rules | 100% | 87.1% | 100% | 0.0% |
| block everything | 100% | 50.8% | 0% | 100% |
| allow everything | 0% | 0% | 0% | 0.0% |
| **BaseDrift** | **100%** | **89.3%** | **79.3%** | **0.0%** |

Measured on the dev split, 624 cases. The holdout agrees: 100% / 87.4% / 79.3%
/ 0.0%.

With both verification channels running, no fraud case in the dev split is
released — by BaseDrift or by the do-nothing baseline, which also catches
sim-swap once it can run a penny drop. So recall ties, and the rules' measurable
contribution is operational: **20.7% of payouts release with no phone call at
all**, and none of the traffic is rejected outright.

**A rejected legitimate vendor is not the same event as a held one.** A
rejection stops the payout and deactivates the fund account; a hold costs a
phone call. Reporting both as one false-positive number hides which one you are
causing, so they are tracked separately — and since v2 the engine cannot reject
anything on its own. `false BLOCK` is 0.0% by construction, not by tuning: the
rules that once rejected now hold and attach a `recommended_action="reject"` for
a human to confirm. 57 held cases on the holdout carry that recommendation and
none of them is legitimate.

Reproduce: `python eval/rules_eval.py`, and `--sweep` for the threshold curve.

### What this evaluation still cannot tell you

Ten authored narratives, randomised within each — but the scenarios and the
rules come from one team's mental model of fraud, so a blind spot would appear
in both the exam and the student. Fraud is oversampled relative to real base
rates for statistical power. `eval/rules_eval.py` prints this under every run,
so no number leaves without it.

---

---

## What the extractor actually costs

`eval/rules_eval.py` assumes perfect extraction. This measures the gap.
`data/render.py` turns each case row into the message a finance team would have
received; `eval/extraction_eval.py` runs the real model over them and compares
the result against the rules-only reference reading.

**That reference is not a ceiling, and this run is what proved it.** The
reference reading is built from the generator's features, and it deliberately
leaves hedging and channel manipulation at clean defaults because the generator
does not model them. The real extractor reads those out of the rendered email,
so it sees *more* than the reference does and can land on either side of it.
On the current corpus the two agree exactly — **89.3% either way on dev** — but
that is the run agreeing, not a bound holding. `rules_eval.py`
already carried the warning that an upper bound the real system beats is not an
upper bound; the wording is now corrected rather than the number explained away.

**The leakage guard comes first, because the README already records this project
making that exact mistake once.** Ablation corpus v1 scored the keyword baseline
at 92.3% because the paraphrases were written first and the trigger lists after.
Rendering an eval corpus is the same trap one layer down. So `BANNED_VOCABULARY`
is imported from the baseline's own trigger lists — retyping it would let the two
drift — and a hit is a hard failure that stops rendering, not a warning. The
baseline is then re-run over the finished corpus:

```
keyword baseline over the rendered corpus:  0 / 624 = 0.0%
```

Meaning has to be inferred from these messages, not pattern-matched. (Its
intent-only figure of 81.5% is the baseline's degenerate rule showing through —
it labels anything containing an account number as a change, so it gets all 507
changes right and all 115 follow-ups wrong.)

### Measured — both splits in full, 1 run, `openai/gpt-oss-120b`

900 documents, every one of them extracted. Not a sample.

| | dev (624) | holdout (276) |
|---|---|---|
| intent | **100%** | **100%** |
| action | **100%** | **100%** |
| scope | **100%** | **100%** |
| all three exact | **100%** | **100%** |
| account · GSTIN · sender domain | 99.8% · **100%** · **100%** | **100%** · **100%** · **100%** |
| IFSC | 99.0% | 99.6% |
| amount | 100% | 100% |
| urgency (precision / recall) | **100% / 96.1%** | **100% / 96.5%** |
| channel manipulation (precision / recall) | **100% / 88.1%** | **100% / 82.2%** |
| extraction failed | **0.0%** | **0.0%** |

Every semantic field is exact on all 900 documents, and every claim except
IFSC is exact on at least one split, with no extraction failures on either.

**End to end the extractor costs nothing, on both splits:**

| | recall | precision | false BLOCK | same rule as ideal |
|---|---|---|---|---|
| dev — rules-only reference | 100% | 89.3% | 0.0% | — |
| **dev — with real extraction** | **100%** | **89.3%** | **0.0%** | **99.5%** |
| holdout — rules-only reference | 100% | 87.4% | 0.0% | — |
| **holdout — with real extraction** | **100%** | **87.4%** | **0.0%** | **98.9%** |

Prompt `b91cb054b107`, renderer 2.0.0, 900 documents extracted, 0 failures.

That is not luck, it is the architecture doing what it was built to do. Identity
never comes from the extracted claims — the destination is read from the payout's
own fund account — so a misread account number cannot move a decision. And the
signals the model *does* miss are corroborating ones: channel manipulation at
85.8% recall can downgrade a rejection recommendation to a plain hold, never a
hold to a release.

**The weakest narrative was `legit_add_account`, and it is fixed.** It scored
23/25 on dev and 9/12 on the holdout, every miss being `ADD_FUND_ACCOUNT` read
as `REPLACE_PAYOUT_DESTINATION` or as `NONE` — the distinction R4's design rests
on. The prompt now states a procedure rather than another definition: *after the
change described, will any payment still reach the old account?* Measured over
three runs on the 25 dev ADD cases, 22.3/25 (89.3%, range 21–24) became 25/25
(100%, no spread), and the holdout — which the fix was never tuned against —
went 9/12 to **12/12**.

The first version of that fix scored the same 100% while quoting `render.py`'s
own template almost verbatim, which would have measured memorisation of the test
corpus rather than the rule. Rewriting it abstractly reproduced the result, and
a test now fails if any four consecutive words of the prompt appear in a
renderer template.

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

**The two that are not perfect, and why they are left that way.**

`sender_domain` at 96–97% is the normalisation issue described above: the model
returns `accounts@vendor.com` where a registrable domain was asked for. It is
corrected before the domain reaches `check_domain`, so it costs nothing, and the
raw recovery number is reported rather than the post-normalisation one.

**Both judgement fields are at 100% precision, and getting there meant fixing
the measurement before the model.** Urgency and channel manipulation are the
only two signals a human would call a matter of opinion; every other field is
read straight off the page. Both now sit at 100% precision with recall in the
80s-90s, and neither number could have been obtained by tuning, because in both
cases the corpus could not initially express the thing being measured.

Urgency scored **100% recall against four hand-written sentences** — a spelling
test with four words. Ten registers later, including two with no clock in them
at all ("our managing director is asking about this one personally"), real
recall is 96%. Its precision came from replacing 62 *accidental* negatives —
leftover wording in ordinary templates — with 105 deliberate ones, then asking
the model whether the sender is trying to compress the buyer's decision rather
than whether a date appears.

**Channel manipulation needed the same correction first.** It sat at ~80% precision and ~68% recall. The corpus
contained no message that used reply/thread/inbox language *without* being
manipulation, so precision was pinned at 100% for every possible detector and
nothing could be evaluated. Adding controls — ordinary mail using the same
vocabulary while widening who can see the exchange — dropped the model to 80.3%
and showed all 13 false positives came from two templates.

Both of those satisfy the old definition, "redirects communication away from an
existing channel", *literally*: directing you to a shared mailbox redirects away
from writing to an individual. The model was following its instruction; the
labels encoded a different rule. The definition now asks **which direction
visibility moves**, and scopes the question to the sender's own payment
correspondence.

Result: **100% precision on all 900 documents, recall 85.8% / 88.3%** — better on
both axes, and it restored `real == ideal` end to end, which those false
positives had broken.

Caveats that travel with these numbers: one run per split, one model. The
extractor is not reproducible run to run — GSTIN recovery swung 13 points
between two passes of the same 30 cases while the "regression" being chased was
1.1 points — so a single pass is one sample, and `--runs N` reports the spread.

---

---

## The held-out result — v2

**276 cases, the full split**, both rules-only and end to end with the live
model, scored 2026-09-02.

**This is a re-score, and the distinction matters.** 249 of these cases were
scored in August and that result has been read. What is genuinely unseen here is
the trust-store rule described below — designed and measured entirely on dev —
and the 29 cases appended with it. "We re-scored the holdout after a rule change
developed on dev" is what happened; "fresh holdout" would not be true.

| | v2 dev (624) | **v2 holdout (276)** | null baseline |
|---|---|---|---|
| recall | 100% | **100%** | 100% |
| precision | 89.3% | **87.4%** | 86.3% |
| **false BLOCK** | 0.0% | **0.0%** | 0.0% |
| held | 79.3% | **79.3%** | 100% |
| false hold | 12.4% | **14.6%** | 16.1% |

End to end, with the real model on all 276: **recall 100%, precision 87.4%,
false BLOCK 0.0%** — identical to the perfect-extraction upper bound, agreeing
on the rule fired in **98.9%** of cases. Zero extraction failures on 276
documents. Intent 99.6%, scope 99.6%, action 98.9%; every misread is `ADD` read
as something else, and none changes an outcome.

**The line the rule was written for**, on data it had never seen:

| scenario | n | outcome |
|---|---|---|
| `fraud_exploit_planted_account` | 15 | 15 held |
| `legit_switch_to_known_account` | 15 | 14 released, 1 held |

An attacker who gets an account onto the vendor master with one accepted email,
waits, and then asks for the money to go there passes every identity check
honestly — the account really is on file, the name really matches, the sender
really is the supplier's own domain because the mailbox is compromised rather
than spoofed. The trust store is not fooled; it was poisoned earlier and is
answering correctly about a fact that is itself the fraud.

When that scenario was added, **19 of 35 such cases were released and recall
fell to 93.8%**. The engine treated "on file" as one thing. It no longer does:
a destination that has never been verified by anything outside email *and* has
never settled a payout is INCONCLUSIVE, so it holds and is never rejected.
Recall returned to 100% — earned this time, on a corpus that had just
demonstrated it could fail.

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

**Precision 87.4% against a null baseline of 86.3%, and false hold 14.6%
against 16.1%.** On accuracy, the rule table is barely distinguishable from
holding every payout and phoning every vendor. That was true in v1, it was true
on both v2 corpora, and no amount of work in v2 changed it.

What the rules actually buy is the **release rate**: 20.7% of payouts clear with
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

---

## How often does this actually fire?

Worth being concrete, because the answer is *not* "constantly", and the case for
the system does not rest on volume.

`python eval/base_rates.py` derives this from the measured corpus rather than
assuming it. The corpus splits cleanly into traffic that requests a destination
change and traffic that does not, and the two behave nothing alike:

| | n | held |
|---|---|---|
| routine payout, no change requested | 158 | **0.0%** |
| change request, legitimate | 286 | 20.3% |
| change request, fraudulent | 456 | 100% |

Real traffic is overwhelmingly the first row, and the null baseline holds all of
it. At 20,000 payouts/day with 0.2% carrying a change:

| per day | BaseDrift (measured) | BaseDrift (pessimistic) | hold everything |
|---|---|---|---|
| released with no call | **19,991** | 19,792 | 0 |
| held, a human must act | **9** | 208 | 20,000 |
| of those, actually fraud | 0.8 | 0.8 | 0.8 |
| legitimate payments cancelled | 0 | 0 | 0 |

**The weakest input, stated before the conclusion rather than after it.** That
first row — the routine hold rate — is the single most important number here,
and on this corpus it rests on **zero events**: no routine payout was held
across either split. Zero events does not mean a zero rate. It means the corpus
is too small to have observed one, and every daily figure scales linearly across
the whole confidence band.

So the table above reports two columns rather than one. The "pessimistic" column
is `--routine-hold 0.01`, assuming 1% of ordinary payouts get held for reasons
this corpus never produced. **The conclusion survives either way**, which is the
only reason it is worth stating: 9 calls a day or 208, against 20,000.

**Both catch every fraud case in this corpus.** That difference — a factor of
somewhere between 96 and 2,200 — is what a precision ratio hides: on a corpus
that is half fraud the two look three points apart; on traffic anyone actually
runs, one is a staffed desk and the other is impossible.

**And the honest other half:** even 208 calls a day is real work, roughly 17
hours of it, so a team. The system does not remove the work; it makes the work
possible and points it at the right payouts. An operator working that queue sees
a genuine attempt rarely, which is exactly the condition under which people
start rubber-stamping. That the queue is *sorted* — a rejection recommendation
attached to the cases carrying real evidence — matters more at that ratio than
any accuracy figure here.

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
