# BaseDrift — What broke

A review of this repository found four ways the decision layer could release a
payout it should not have. All four are fixed and carry regression tests that
fail against the previous engine.

They are written down rather than quietly patched, because **a control that
hides its own near-misses is the failure mode it exists to prevent.** The
second half of this document is the list of things still wrong, or right for
reasons that are weaker than they look.

**[← README](README.md)** · **[Evaluation](EVALUATION.md)** · **[Rulebook](RULEBOOK.md)** · **[Build log](BUILD-LOG.md)**

---

## What the audit found

A review of this repository found four ways the decision layer could release a payout
it should not have. All four are fixed and carry regression tests that fail against
the previous engine. They are documented rather than quietly patched, because a
control that hides its own near-misses is the failure mode it exists to prevent.

**The LLM could produce an ALLOW by itself.** R2 returned ALLOW the moment the
extractor reported `intent == PAYMENT_FOLLOWUP`, before the Tier 1 checks were
constructed — no FAV, no GSTIN, no continuity. Measured against the old engine: a
request with an unseen destination account, FAV reporting the account *inactive*, a
name match of 3/100, an attacker-controlled domain and urgency language returned
`ALLOW` with `payout_allowed=True`. A prompt injection that reached that one label was
a total bypass.

R2 now verifies the claim instead of accepting it. "Nothing is changing" is checked
against the real destination: known account → ALLOW, unknown → hold, another vendor's
account → block. Demonstrated end to end — the identical follow-up email allows when
the payout points at the vendor's known account and holds when it points anywhere
else.

**FAV `account_status` was never read.** It was carried on `FAVResult`, logged, and
printed, but consumed by nothing. A change request against an account the bank
reported **inactive**, with a name match of 99, returned `R7_all_clear` — "all
identity checks passed". It is now a Tier 1 signal: active passes, inactive fails,
unknown warns, because an inconclusive FAV is not a clean one.

**The account checked was claimed, not actual.** Continuity tested the account number
the LLM read out of the email; the payout's real destination was never passed into the
decision at all — the old `decide()` had no parameter for it. A message naming the
vendor's genuine account while the payout pointed elsewhere passed cleanly. The
resolved destination now comes from the payout's fund account, the message's claim is
a labelled fallback for offline analysis only, and every audit record states which of
the two was validated.

**Contextual signals alone could reject a legitimate vendor.** R4 blocked on
`REPLACE + new account + any 2 Tier-2 warns`. Both inputs are true of an
ordinary legitimate bank change — a new account is what changing banks *means*,
and urgency plus an unfamiliar domain is what an acquired company's finance team
looks like. It also contradicted the rule table's own stated invariant that
Tier 2 is supporting evidence and never decisive alone.

Measured: **it rejected 15.8% of all legitimate traffic** — 49 of 60
acquisition/rebrand cases — and no threshold repaired it. Tightening to 4 warns
cut false blocks to 0.6% but dropped recall to 85.4%, level with doing nothing.

R4 now requires evidence of *deliberate impersonation* — currently a typosquat
domain, carried as an extensible `deception` flag on `Signal` — corroborated by
at least one contextual signal. False blocks fell to **0.6%** and precision rose
from 74.5% to 87.5%.

The alternative was tested rather than assumed: treating any domain that embeds
the vendor's name as deception recovers recall to 97.9% but returns false blocks
to 15.8%, because a rebrand embeds the vendor's name too. That ambiguity is real
rather than a detector gap — see the limit stated below.

**The injection filter missed the two best hiding places.** `sanitize()`
enumerated eight characters — zero-widths, soft hyphen, the LTR/RTL *marks*,
BOM — and its docstring called them "the characters used to hide instructions
inside documents". Two classes survived it:

- **U+202A–202E and U+2066–2069**, the bidirectional embeddings, overrides and
  isolates. An RLO makes text render in an order entirely different from how it
  reads.
- **U+E0000–E007F**, the Unicode Tag block. Wholly invisible, and the basis of
  ASCII smuggling — arbitrary instructions riding inside innocuous text.

Enumerating characters means the list is only ever as good as the last threat
someone remembered, so filtering is now by Unicode **category** (`Cf` and `Cc`,
plus the tag block explicitly), which closes the class rather than patching it.
Newline and tab are preserved because they carry document structure.

The count of what was removed is now reported rather than discarded: a
legitimate invoice email contains none of these, so their presence is evidence
about the document. Oversized documents are flagged too — padding the front
pushes the real request past the size cap, and a model that read only part of a
document is inconclusive, not clean. Neither feeds a rule yet; the generator
emits no such cases, and rules do not change without dataset coverage.

**Missing data was counted as evidence of fraud.** `Signal` had three states,
and `WARN` was carrying two incompatible meanings: "I checked this and it looks
wrong" and "I could not check this". A message that simply omitted an amount
earned a Tier-2 risk signal, which fed R4's BLOCK threshold — so a case could be
pushed toward rejection for carrying *less* information rather than worse
information. That was the third of the three warns on the hero case.

Signals now have four states, with `INCONCLUSIVE` distinct from `WARN`. Both
still hold a payout — "couldn't check" must never read as "clean" — but only
adverse evidence can contribute to a rejection. The hero case still blocks, now
citing two real signals instead of three padded ones.

**Hedge detection failed open on spelling.** `check_gstin` decided PASS-versus-WARN by
testing `hedged_fields` against the exact tuple `("gstin", "proposed_gstin")`. The
model emits `gst_number` and `gst` for the same concept, and both were silently
missed — and a missed hedge means one fewer WARN, biasing toward release. The hero
case demonstrated it live, reporting `gstin PASS` on "should be the same as before".
Matching is now on the concept, and that same case now correctly warns.

---

---

## Known open flaws

**R4 claimed corroboration it did not have — fixed.** The BEC rule required
impersonation evidence *plus* at least one Tier 2 warning to corroborate it. But
the only signal that sets `deception` is `sender_domain`, and that signal is
itself a Tier 2 warning — so the second clause was satisfied by the first and
never constrained anything. Deleting Tier 2 from the engine entirely produced
numbers identical to deleting R6 alone, which is how it surfaced.

The audit record was the real casualty: the reason string read *"corroborated by
2 contextual risk signal(s) (sender_domain, urgency)"*, counting the
impersonation among the things corroborating it, and on 4 of 62 dev firings
`sender_domain` was the only Tier 2 warning there was. Corroboration must now
come from a different signal. R4 fires 58 instead of 62 on dev and 24 instead of
26 on holdout; **recall stays 100%** because those cases are still held, by R5 or
R6 — what they lose is the rejection recommendation, which is exactly the thing
that ought to need corroborating.

**Tier 2's other job catches nothing on this corpus.** Until the
scenarios described under *The held-out result* were added, `R6_contextual_risk`
and `R7_all_clear` had fired **zero times in 552 cases** — every case was caught
by an earlier rule, so the four contextual checks and every inbox signal were
computed, stored and displayed while being unable to affect an outcome.

They fire now. R6 fires 15 times on dev and **all 15 are legitimate** — urgent
language on an ordinary account switch. With the trust-store anchor in place,
tier 2 costs 15 held legitimate cases and catches nothing tier 1 does not already
catch. It stays because the anchor check returns nothing when the vendor master
carries no provenance columns, which is the likely case on real merchant data —
so it is defence in depth whose depth is currently measurable at zero on
synthetic data with a complete master. **Do not quote tier 2 as catching fraud
here.** It does not.

**A history-based inbox signal was retired rather than re-tuned.**
`inbox_repeat_destination_requests` fired on 70.0% of legitimate change requests
against 36.8% of fraud — it was measuring how long a relationship had existed,
because a typosquat has no history at all. Three variants were measured and the
best flipped the direction by eight points, which is noise with a preference. The
ceiling is the corpus: 552 change requests over 90 days across 301 domains means
107 domains ask to move the destination twice or more in one quarter, where a
real supplier does it once in several years. `prior_change_requests` remains an
MCP tool — the agent may still ask — but nothing turns the answer into a hold.


**A compromised callback was unrecoverable from evidence — so a second channel
was added.** When the attacker controls the vendor's phone as well as the mail,
the callback confirms the fraud. 17 cases released on the dev set, and no amount
of rule tuning could reach them: they are identical to a genuine rebrand on every
observable signal.

The fix is not another heuristic. A phone number can be taken; **the account we
have already been paying cannot** — moving money away from it is the entire point
of the attack. So the second channel is a **Reverse Penny Drop from the account
already on file**: send ₹1 from where we have been paying you.

The obvious rule for combining them is wrong. SIM-swap fraud has the callback
*passing* — the attacker answers the phone — so "either channel confirms" releases
exactly the cases the channel was added for. The penny drop is therefore
authoritative and the callback corroborates, mirroring the Tier 1 / Tier 2 split
in the rule table.

Measured on the dev set:

| | callback only | with the penny drop |
|---|---|---|
| `fraud_sim_swap` released | **17** | **0** |
| `legit_unreachable` held | **30** | **0** |
| recall | 92.9% | **100%** |
| false BLOCK | 0.6% | **0.6%** |
| false hold | 9.5% | 11.7% |

It closes the gap in both directions: no sim-swap case survives it, and thirty
genuine vendors who simply could not answer a phone are no longer held for one.

**The honest cost.** A vendor who genuinely closed their old account fails the
penny drop through no fault of their own and lands beside the attacker in a state
the evidence cannot separate. Both are held for a human rather than released —
false holds rose 9.5% → 11.7%, and **rejections did not move at all**. That is the
trade taken deliberately: a hold is recoverable, a release is not.

**And it raises the floor for everyone, including doing nothing.** The null
baseline also reaches 100% recall now, because holding every payout and running
both channels catches sim-swap too. So the rules' measurable contribution reverts
to what it was before: the same capture at fewer verification cycles.

*The figures in this section are v1's, kept as the record of why the second
channel was added.* On the v2 corpus the same comparison is **79.3% held against
the null baseline's 100%** — 20.7% of payouts release with no phone call. The
margin narrowed because v2 stopped rejecting anything outright, which converts
former rejections into holds; that is the trade described above, taken
deliberately.

**FIXED IN v2.** The limitation below is the v1
behaviour and the reason for the rebuild. `vendor_accounts.csv` now carries each
account with its own provenance — `added_via`, `verified_by`,
`settled_payout_count` — `build_account_index()` returns a set of owners rather
than overwriting, and `group_id` distinguishes a declared corporate group from
the mule pattern. Measured on the v2 dev split: corporate groups sharing an
account are allowed (21 of 22), and a payout to a legitimate second account is
allowed (21 of 21) where v1 held it on every payout, forever.

**The vendor master holds one account per vendor, and real ones do not.**
`VendorRecord` has an `additional_accounts` field and `all_known_accounts()`
reads it — but nothing populates it, no CSV column carries it, and all 120
vendors have exactly one account. It is a coded capability with no data behind
it, the same shape `account_status` had before it was fixed.

Two things follow, and the first is reproducible today. `build_account_index()`
maps account to vendor with a plain assignment, so when two vendors share an
account the second **silently overwrites** the first — and a payout to that
shared account then fires `R2c_followup_destination_conflict` and is **rejected**.
`decision_engine.py`'s own comment says sharing an account across contacts is
legitimate for corporate groups; the code blocks it. Second, a vendor with a
genuine second account — separate divisions, a collections and a refunds
account — gets a hold on every payout to it, permanently, because the master
records only one.

It also makes `legit_add_account` incoherent as a scenario: the request to add
an account is modelled, the resulting state never is, so ADD versus REPLACE
cannot be tested end to end even though R4's design rests on the distinction.

The fix is a separate `vendor_accounts` table carrying each account's own
attributes, a many-to-many index, and an explicit group id so that a shared
account inside a declared group is distinguishable from the mule pattern. The
important column is **`verified_by`**: the vendor master is the root of trust
for every check here, so an account that entered it without verification is
worthless as an anchor — the destination would be checked against a record an
attacker could have written. Scoped in BUILD-LOG.md as V2.0.

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

**Not yet connected to Razorpay.** Covered in full below — the decision layer
is real, the integration is not.

---
