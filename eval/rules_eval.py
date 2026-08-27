"""
PayeeProof — rules evaluation.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
========================================
This scores the DECISION ENGINE ONLY. It builds each case's ExtractionResult
directly from the generator's ground-truth features, so the LLM never runs.

  - A failure here is the rule table's fault. No prompt or renderer fixes it.
  - A pass here says nothing about whether the extractor can recover those
    features from real prose. That is a separate question, measured separately.

Do not quote this number as end-to-end performance. It is an upper bound the
extractor can only erode.

Free, instant, deterministic — which is the point. Threshold tuning that needs a
live model costs an hour per iteration and is not reproducible run to run.


WHY ACCURACY IS NOT THE HEADLINE
================================
Every case carries `callback_reaches_known_contact`, and the generator sets it
False for fraud and True for legit. The callback therefore separates the classes
perfectly on its own. A pipeline that runs NO RULES and simply holds every payout
for a callback scores 100% recall and 100% precision on this dataset.

So accuracy cannot distinguish PayeeProof from doing nothing. What the rules
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
    "legit_easy", "legit_hard", "legit_rebrand", "legit_add_account",
    "legit_unreachable",
)


# ── Loading ───────────────────────────────────────────────────────────

def load_vendors():
    path = os.path.join(DATA, "vendor_master.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    vendors = {
        r["vendor_id"]: VendorRecord(
            vendor_id=r["vendor_id"], legal_name=r["legal_name"], gstin=r["gstin"],
            known_domain=r["known_domain"], known_phone=r["known_phone"],
            known_account_number=r["known_account_number"],
            known_ifsc=r["known_ifsc"],
            avg_payout_amount=float(r["avg_payout_amount"]),
        )
        for r in rows
    }
    index = {v.known_account_number: vid for vid, v in vendors.items()}
    return vendors, index


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
    def stepup_rate(self):
        return self.outcomes[STEP_UP] / self.n if self.n else 0.0

    @property
    def block_rate(self):
        return self.outcomes[BLOCK] / self.n if self.n else 0.0


def evaluate(rows, vendors, index, decide_fn, name):
    r = Result(name, len(rows))
    for row in rows:
        vendor = vendors[row["vendor_id"]]
        dec = decide_fn(row, vendor, index)
        r.outcomes[dec.outcome] += 1
        r.rules[dec.rule_fired] += 1

        ver = verifier.verify(dec, vendor,
                              row["callback_reaches_known_contact"] == "True",
                              row["case_id"])
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

def payeeproof(row, vendor, index):
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
                  destination_account_number=row["proposed_account_number"])


def _fixed(outcome, allowed):
    def fn(row, vendor, index):
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
    print("This measures the DECISION ENGINE only. It is an upper bound;")
    print("the extractor can only erode it. Not an end-to-end figure.")
    print()
    print(f"  {'system':36s} {'recall':>7s} {'prec':>6s} {'step-up':>8s} "
          f"{'FALSE BLOCK':>12s} {'false hold':>11s}")
    print("  " + "-" * 84)
    for r in results:
        star = " *" if r.name.startswith("PayeeProof") else "  "
        print(f"{star}{r.name:36s} {r.recall:7.1%} {r.precision:6.1%} "
              f"{r.stepup_rate:8.1%} {r.false_block_rate:11.1%} {r.false_hold_rate:11.1%}")
    print()
    print("  recall      = fraud not released      FALSE BLOCK = legit REJECTED (severe)")
    print("  step-up     = callbacks required      false hold  = legit held for review")
    print()

    pp = next(r for r in results if r.name.startswith("PayeeProof"))
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
    if pp.missed_fraud_allowed:
        print(f"  {pp.missed_fraud_allowed} fraud case(s) RELEASED.")
    print()

    print("  PayeeProof, final outcome by scenario   (! marks a wrong outcome):")
    for t in SCENARIOS:
        d = pp.by_scenario[t]
        tot = sum(d.values())
        if not tot:
            continue
        fraud = t.startswith("fraud")
        parts = []
        for k, v in sorted(d.items()):
            bad = (fraud and k == ALLOW) or ((not fraud) and k == BLOCK)
            parts.append(f"{'!' if bad else ''}{k}={v}")
        print(f"    {t:20s} n={tot:4d}   {'  '.join(parts)}")
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
        def fn(row, vendor, idx, _k=k):
            d = payeeproof(row, vendor, idx)
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

    results = [evaluate(rows, vendors, index, payeeproof, "PayeeProof, full rule table")]
    results += [evaluate(rows, vendors, index, fn, nm) for nm, fn in BASELINES]
    report(results, args.split, len(rows))


if __name__ == "__main__":
    main()
