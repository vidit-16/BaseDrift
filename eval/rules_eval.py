"""
BaseDrift — rules evaluation.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
========================================
This scores the DECISION ENGINE ONLY. It builds each case's ExtractionResult
directly from the generator's ground-truth features, so the LLM never runs.

  - A failure here is the rule table's fault. No prompt or renderer fixes it.
  - A pass here says nothing about whether the extractor can recover those
    features from real prose. That is a separate question, measured separately.

Do not quote this number as end-to-end performance. It is a REFERENCE reading,
not a ceiling: features_to_extraction leaves hedging and channel manipulation at
clean defaults because the generator does not model them, so the real extractor
sees MORE of the rendered email than this does and can land either side of it.
Measured on dev with prompt 6b4bcefc0560: real precision 86.6% against this
reading's 86.4%.

Free, instant, deterministic — which is the point. Threshold tuning that needs a
live model costs an hour per iteration and is not reproducible run to run.


WHY ACCURACY IS NOT THE HEADLINE
================================
Every case carries `callback_reaches_known_contact`, and the generator sets it
False for fraud and True for legit. The callback therefore separates the classes
perfectly on its own. A pipeline that runs NO RULES and simply holds every payout
for a callback scores 100% recall and 100% precision on this dataset.

Since V2.1 the engine cannot reject anything: the harshest outcome it reaches is
a hold, and rules that once rejected now attach a recommendation a human acts
on. So the step-up column went UP and the FALSE BLOCK column went to zero, and
neither movement is an accuracy result. What the rules still buy over holding
everything is the release rate — the payouts that never need a phone call.

So accuracy cannot distinguish BaseDrift from doing nothing. What the rules
actually buy is a lower STEP-UP RATE: the same fraud capture while phoning the
vendor far less often. Every run below is reported against that null baseline,
because a number without it is meaningless.

Run:
    python eval/rules_eval.py
    python eval/rules_eval.py --sweep       # R4 Tier-2 threshold sensitivity
    python eval/rules_eval.py --split holdout
"""

import argparse
import collections
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import pipeline  # noqa: E402
import verifier  # noqa: E402
from decision_engine import (  # noqa: E402
    decide, Decision, FAVResult, VendorRecord,
    ALLOW, STEP_UP, BLOCK, WARN,
)
from extractor import (  # noqa: E402
    ExtractionResult,
    INTENT_CHANGE, INTENT_FOLLOWUP,
    ACTION_REPLACE, ACTION_ADD, ACTION_NONE,
    SCOPE_BOTH, SCOPE_FUTURE, SCOPE_NONE,
)

DATA = os.path.join(HERE, "..", "data")
SCENARIOS = (
    "fraud_easy", "fraud_hard", "fraud_compromised", "fraud_mule", "fraud_sim_swap",
    "fraud_planted_account", "fraud_first_contact", "fraud_thread_hijack",
    "legit_easy", "legit_hard", "legit_rebrand", "legit_add_account",
    "legit_unreachable", "legit_group_shared_account", "legit_second_account",
    "legit_added_then_paid",
)


# ── Loading ───────────────────────────────────────────────────────────

def load_vendors():
    """
    Delegates to pipeline, rather than reimplementing the read.

    This used to build its own index as {primary_account: vendor_id}, which
    silently disagreed with pipeline's in two ways at once: it ignored every
    non-primary account, and it overwrote on collision. An evaluator that loads
    the trust store differently from the system under test is measuring
    something that does not exist.
    """
    vendors = pipeline.load_vendors(os.path.join(DATA, "vendor_master.csv"),
                                    os.path.join(DATA, "vendor_accounts.csv"))
    return vendors, pipeline.build_account_index(vendors)


def load_cases(split):
    path = os.path.join(DATA, f"cases_{split}.csv")
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found.\n"
            f"cases_holdout.csv is deliberately not committed. Regenerate both "
            f"splits with:\n    python data/generate_data.py"
        )
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── The rules-eval contract ───────────────────────────────────────────

def features_to_extraction(row, vendor):
    """
    The semantic reading a FLAWLESS extractor would produce for this narrative.

    This is the contract of a rules eval: hand the engine perfect evidence and
    see what the policy does with it. Fields the generator does not model —
    hedging, channel manipulation — are left at their clean defaults rather than
    invented, so the engine is never credited with signals the data never had.
    """
    act = row["action_type"]
    intent = INTENT_FOLLOWUP if act == "NONE" else INTENT_CHANGE
    action = {"REPLACE": ACTION_REPLACE, "ADD": ACTION_ADD, "NONE": ACTION_NONE}[act]
    scope = {"REPLACE": SCOPE_BOTH, "ADD": SCOPE_FUTURE, "NONE": SCOPE_NONE}[act]

    urgent = row["urgency_language"] == "True"
    channel = row["channel_manipulation"] == "True"
    hedged = row["hedged_gstin"] == "True"

    return ExtractionResult(
        ok=True,
        intent=intent, action=action, scope=scope,
        proposed_account_number=row["proposed_account_number"],
        proposed_ifsc=row["proposed_ifsc"],
        proposed_gstin=row["proposed_gstin"],
        sender_domain=row["sender_domain"],
        sender_phone=row["sender_phone_used"],
        vendor_name_claimed=vendor.legal_name,
        amount=float(row["amount"]),
        urgency_detected=urgent,
        urgency_phrases=["(scenario urgency)"] if urgent else [],
        hedging_detected=hedged,
        hedged_fields=["proposed_gstin"] if hedged else [],
        channel_manipulation_detected=channel,
        channel_manipulation_phrases=["(scenario redirect)"] if channel else [],
    )


# ── Scoring ───────────────────────────────────────────────────────────

class Result:
    def __init__(self, name, n):
        self.name, self.n = name, n
        self.tp = self.fp = self.tn = self.fn = 0
        # A legitimate request BLOCKED is a customer-facing failure: the payout
        # is rejected and the fund account deactivated. One merely HELD is an
        # operational cost a human resolves. Lumping both into "false positive"
        # hides which of the two you are actually causing.
        self.false_block = 0
        self.false_hold = 0
        # V2.1 makes false_block 0 BY CONSTRUCTION — no rule can reject any
        # more — so on its own that column now says nothing. These two keep the
        # number honest: they count the holds the engine wanted to end in a
        # rejection, and how many of those are legitimate. That second figure is
        # what v1 rejected outright and what a human now decides instead.
        self.recommended = 0
        self.recommended_legit = 0
        # V2.6. Channel 2 unavailable is a THIRD state, not a quiet failure,
        # and it needs its own count: if it were large it would mean the
        # seasoning policy is holding legitimate vendors wholesale.
        self.c2_unavailable = 0
        self.c2_named_but_uncontrolled = 0
        self.missed_fraud_allowed = 0
        self.outcomes = collections.Counter()
        self.rules = collections.Counter()
        self.by_scenario = collections.defaultdict(collections.Counter)

    @property
    def recall(self):
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def precision(self):
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def false_block_rate(self):
        legit = self.fp + self.tn
        return self.false_block / legit if legit else 0.0

    @property
    def false_hold_rate(self):
        legit = self.fp + self.tn
        return self.false_hold / legit if legit else 0.0

    @property
    def recommended_rate(self):
        return self.recommended / self.n if self.n else 0.0

    @property
    def stepup_rate(self):
        return self.outcomes[STEP_UP] / self.n if self.n else 0.0

    @property
    def block_rate(self):
        return self.outcomes[BLOCK] / self.n if self.n else 0.0


def evaluate(rows, vendors, index, decide_fn, name):
    r = Result(name, len(rows))
    for row in rows:
        vendor = vendors[row["vendor_id"]]
        dec = decide_fn(row, vendor, index, vendors)
        r.outcomes[dec.outcome] += 1
        r.rules[dec.rule_fired] += 1
        if dec.recommended_action == "reject":
            r.recommended += 1
            if row["label"] != "fraud":
                r.recommended_legit += 1

        # The CSV says which accounts the requester can actually send from. It
        # does NOT say which account should be demanded — that is the policy's
        # job, and keeping the two apart is what makes the policy measurable.
        # v1's single controls_existing_account bool answered both questions at
        # once, which is why the planted-account hole stayed invisible.
        controls = [a for a in
                    (row.get("requester_controls_accounts") or "").split(";") if a]
        ver = verifier.verify(dec, vendor,
                              row["callback_reaches_known_contact"] == "True",
                              row["case_id"],
                              requester_controls_accounts=controls,
                              as_of=row.get("request_date") or None,
                              requested_via="email_request")
        if ver is not None:
            if ver.outcome == verifier.UNAVAILABLE_C2:
                r.c2_unavailable += 1
            elif (ver.verification_account
                  and ver.verification_account not in controls and controls):
                r.c2_named_but_uncontrolled += 1

        final = ver.final_outcome if ver else dec.outcome
        allowed = ver.payout_allowed if ver else dec.payout_allowed
        caught = not allowed

        if row["label"] == "fraud":
            if caught:
                r.tp += 1
            else:
                r.fn += 1
                r.missed_fraud_allowed += 1
        else:
            if caught:
                r.fp += 1
                if final == BLOCK:
                    r.false_block += 1
                else:
                    r.false_hold += 1
            else:
                r.tn += 1
        r.by_scenario[row["scenario_type"]][final] += 1
    return r


# ── Decision functions under test ─────────────────────────────────────

def basedrift(row, vendor, index, vendors=None):
    """
    `vendors` is the whole master, and it is a PARAMETER rather than a module
    global for a reason that cost a measurement.

    It was a global, set inside evaluate(). eval/extraction_eval.py imports this
    function and calls it directly, never going through evaluate() — so the
    global stayed None, decide() lost the ability to tell a shared account
    inside a DECLARED group from the mule pattern, and the "ideal" upper bound
    reported 79.0% precision against the 85.9% rules_eval measured on the same
    cases. An upper bound that the real system beats is not an upper bound; it
    is a broken baseline, and it read as the v1 behaviour that rejected
    corporate groups.
    """
    ext = features_to_extraction(row, vendor)
    # FAV is a live dependency: sometimes unavailable, sometimes reporting a
    # non-active account. Both branches previously had zero dataset coverage.
    available = row["fav_name_available"] == "True"
    fav = FAVResult(row["fav_account_status"],
                    row["registered_name_returned"] if available else None,
                    int(row["name_match_score"]) if available else None)
    return decide(ext, fav, vendor,
                  other_vendor_accounts=index,
                  near_duplicate=row["near_duplicate_invoice"] == "True",
                  split_below=row["split_below_threshold"] == "True",
                  destination_account_number=row["proposed_account_number"],
                  vendors=vendors)


def _fixed(outcome, allowed):
    def fn(row, vendor, index, vendors=None):
        return Decision(outcome=outcome, rule_fired=f"BASELINE_{outcome}",
                        reason="baseline", triggered_by=[],
                        payout_allowed=allowed, needs_callback=(outcome == STEP_UP))
    return fn


BASELINES = [
    ("null — hold everything, run no rules", _fixed(STEP_UP, False)),
    ("allow everything", _fixed(ALLOW, True)),
    ("block everything", _fixed(BLOCK, False)),
]


# ── Reporting ─────────────────────────────────────────────────────────

def report(results, split, n):
    print()
    print("=" * 78)
    print(f"RULES EVAL — {split} split, {n} cases, perfect extraction (no LLM)")
    print("=" * 78)
    print("This measures the DECISION ENGINE only, on a reference reading —")
    print("not a ceiling, and not an end-to-end figure. See the module docstring.")
    print()
    print(f"  {'system':36s} {'recall':>7s} {'prec':>6s} {'held':>7s} "
          f"{'rec.rej':>8s} {'FALSE BLOCK':>12s} {'false hold':>11s}")
    print("  " + "-" * 92)
    for r in results:
        star = " *" if r.name.startswith("BaseDrift") else "  "
        print(f"{star}{r.name:36s} {r.recall:7.1%} {r.precision:6.1%} "
              f"{r.stepup_rate:7.1%} {r.recommended_rate:8.1%} "
              f"{r.false_block_rate:11.1%} {r.false_hold_rate:11.1%}")
    print()
    print("  recall      = fraud not released      FALSE BLOCK = legit REJECTED (severe)")
    print("  held        = payout not released     false hold  = legit held for review")
    print("  rec.rej     = held, and the engine recommends a human reject it")
    print()

    pp = next(r for r in results if r.name.startswith("BaseDrift"))
    null = next(r for r in results if r.name.startswith("null"))
    delta = pp.recall - null.recall
    if abs(delta) < 1e-9:
        saved = null.stepup_rate - pp.stepup_rate
        print(f"  Recall ties the null baseline ({pp.recall:.1%}); the rules buy a "
              f"{saved:.0%} lower step-up rate.")
    else:
        print(f"  Recall vs null baseline: {pp.recall:.1%} vs {null.recall:.1%} "
              f"({delta:+.1%}).")
        if delta > 0:
            print(f"  The rules catch {pp.tp - null.tp} fraud case(s) the callback "
                  f"alone does not.")
    if pp.false_block:
        print(f"  {pp.false_block} legitimate request(s) REJECTED outright — "
              f"{pp.false_block_rate:.1%} of all legitimate traffic.")
    else:
        print("  0 legitimate requests rejected — but read that as 0 BY "
              "CONSTRUCTION, not as an")
        print("  accuracy result: no rule can reject any more (V2.1). The number "
              "that replaces it:")
        legit = pp.fp + pp.tn
        print(f"  {pp.recommended} held case(s) carry a rejection recommendation, "
              f"{pp.recommended_legit} of them legitimate")
        print(f"  ({pp.recommended_legit / legit if legit else 0:.1%} of legitimate "
              f"traffic). v1 would have rejected exactly those unattended;")
        print("  they now wait for a human, which is the whole of the change.")
    if pp.missed_fraud_allowed:
        print(f"  {pp.missed_fraud_allowed} fraud case(s) RELEASED.")
    print()

    print(f"  Channel 2: {pp.c2_unavailable} case(s) had NO account qualifying to "
          f"prove control")
    print(f"             ({pp.c2_unavailable / n:.1%}); those escalate and never "
          f"fall back to the callback.")
    print(f"             {pp.c2_named_but_uncontrolled} case(s) controlled SOME "
          f"account on file but not the")
    print(f"             one this system named — the planted-account pattern.")
    print()

    print("  BaseDrift, final outcome by scenario   (! marks a wrong outcome):")
    # SCENARIOS fixes the READING ORDER, not the contents. It used to fix both,
    # and when two scenarios were added to the generator they were evaluated
    # correctly and then silently omitted from this table — 248 of 278 cases
    # displayed, with nothing saying so. A report that can quietly drop a
    # scenario is worse than one that prints it in an odd order.
    seen = [t for t in SCENARIOS if pp.by_scenario.get(t)]
    extra = sorted(t for t in pp.by_scenario
                   if t not in SCENARIOS and pp.by_scenario[t])
    shown = 0
    for t in seen + extra:
        d = pp.by_scenario[t]
        tot = sum(d.values())
        if not tot:
            continue
        fraud = t.startswith("fraud")
        parts = []
        for k, v in sorted(d.items()):
            bad = (fraud and k == ALLOW) or ((not fraud) and k == BLOCK)
            parts.append(f"{'!' if bad else ''}{k}={v}")
        shown += tot
        print(f"    {t:30s} n={tot:4d}   {'  '.join(parts)}")
    assert shown == pp.n, (
        f"the scenario table shows {shown} of {pp.n} cases; a scenario in the "
        f"data has no row here")
    print()
    print("  rule fired:")
    for rule, c in pp.rules.most_common():
        print(f"    {rule:40s} {c:5d}")
    print()

    print("  CAVEAT — read before quoting any number above.")
    print(f"  {len(SCENARIOS)} authored narratives, randomised within each. The scenarios and")
    print("  the rules still share one team's mental model of fraud, so a blind spot")
    print("  would appear in both the exam and the student. Fraud is oversampled")
    print("  relative to real base rates for statistical power.")
    print()


def sweep(rows, vendors, index):
    """R4's Tier-2 threshold vs step-up rate — the knob accuracy cannot see."""
    print()
    print("=" * 78)
    print("R4 TIER-2 THRESHOLD SWEEP")
    print("=" * 78)
    print(f"  {'threshold':>9s} {'recall':>8s} {'prec':>7s} {'step-ups':>10s} "
          f"{'FALSE BLOCK':>12s}")
    print("  " + "-" * 52)
    for k in range(1, 6):
        def fn(row, vendor, idx, vendors=None, _k=k):
            d = basedrift(row, vendor, idx, vendors)
            if d.rule_fired == "R4_bec_pattern":
                n_warn = sum(1 for s in d.tier2 if s.result == WARN)
                if n_warn < _k:
                    return Decision(outcome=STEP_UP, rule_fired="R4_suppressed",
                                    reason="below threshold", triggered_by=[],
                                    tier1=d.tier1, tier2=d.tier2, needs_callback=True)
            return d
        r = evaluate(rows, vendors, index, fn, f"k={k}")
        print(f"  {k:9d} {r.recall:8.1%} {r.precision:7.1%} "
              f"{r.stepup_rate:9.1%} {r.false_block_rate:11.1%}")
    print()
    print("  Read the FALSE BLOCK column against recall: a lower threshold blocks")
    print("  more fraud outright but rejects more legitimate vendors. That is the")
    print("  real trade-off, and it was invisible while every scenario emitted one")
    print("  fixed feature vector.")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("dev", "holdout"))
    ap.add_argument("--sweep", action="store_true",
                    help="R4 Tier-2 threshold sensitivity")
    args = ap.parse_args()

    if args.split == "holdout":
        print()
        print("!" * 78)
        print("You are opening the HOLDOUT split. Per the methodology in README.md")
        print("this is scored ONCE, at the end, and reported with its size whatever")
        print("the result. Do not tune anything against what you are about to see.")
        print("!" * 78)

    vendors, index = load_vendors()
    rows = load_cases(args.split)

    if args.sweep:
        sweep(rows, vendors, index)
        return

    results = [evaluate(rows, vendors, index, basedrift, "BaseDrift, full rule table")]
    results += [evaluate(rows, vendors, index, fn, nm) for nm, fn in BASELINES]
    report(results, args.split, len(rows))


if __name__ == "__main__":
    main()
