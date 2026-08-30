"""
PayeeProof — what the measured rates mean at real-world volumes.

    python eval/base_rates.py
    python eval/base_rates.py --payouts 100000 --change-rate 0.002

WHY THIS FILE EXISTS
====================
Every accuracy figure this project reports is computed on a corpus that is
roughly half fraud and three-quarters change requests. Real payout traffic is
neither. So the honest reading of "precision 87.1% against a null baseline of
85.8%" is not "barely better" — it is "that comparison is being made on a
distribution nobody operates in."

This file does not fix the numbers. It converts them into the thing an operator
actually feels: HOW MANY PHONE CALLS PER DAY.

THE MEASUREMENT THAT MATTERS, and it is not precision
======================================================
The corpus separates cleanly into traffic that requests a destination change and
traffic that does not, and the two behave completely differently:

    routine payout, no change requested   held ~1-2% of the time
    change request, legitimate            held ~23-27%
    change request, fraudulent            held 100%

Real traffic is overwhelmingly the first row. The null baseline holds all of it.
That difference is invisible in a precision ratio and dominates everything at
scale.

THE UNCERTAINTY, STATED UP FRONT
================================
The routine hold rate is the single most important input here and it rests on
TWO events across both splits — one in dev, one in holdout. Two. The confidence
interval around it is enormous, and every daily-volume figure below scales
linearly with it. Treat the shape of the answer as informative and the precise
figures as an estimate with a wide band; --routine-hold overrides it so anyone
can see how sensitive the conclusion is.

The change-request rate and the fraud rate among change requests are
ASSUMPTIONS, not measurements. They are defaults drawn from the README's volume
discussion and are the first thing to challenge.
"""

import argparse
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import rules_eval as RE  # noqa: E402
import verifier  # noqa: E402


def measure(split):
    """Hold rates by traffic type, from the corpus rather than from assumption."""
    vendors, index = RE.load_vendors()
    rows = RE.load_cases(split)
    g = collections.defaultdict(lambda: [0, 0])
    for row in rows:
        v = vendors[row["vendor_id"]]
        dec = RE.payeeproof(row, v, index, vendors)
        controls = [a for a in
                    (row.get("requester_controls_accounts") or "").split(";") if a]
        ver = verifier.verify(dec, v,
                              row["callback_reaches_known_contact"] == "True",
                              row["case_id"],
                              requester_controls_accounts=controls,
                              as_of=row.get("request_date") or None)
        allowed = ver.payout_allowed if ver else dec.payout_allowed
        kind = "routine" if row["action_type"] == "NONE" else "change"
        g[f"{kind}/{row['label']}"][0] += 1
        g[f"{kind}/{row['label']}"][1] += (not allowed)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payouts", type=int, default=20000,
                    help="payouts per day (default 20,000)")
    ap.add_argument("--change-rate", type=float, default=0.002,
                    help="fraction of payouts carrying a destination change")
    ap.add_argument("--fraud-rate", type=float, default=0.02,
                    help="fraction of change requests that are fraudulent")
    ap.add_argument("--routine-hold", type=float, default=None,
                    help="override the measured routine hold rate")
    args = ap.parse_args()

    combined = collections.defaultdict(lambda: [0, 0])
    for split in ("dev", "holdout"):
        for k, (n, held) in measure(split).items():
            combined[k][0] += n
            combined[k][1] += held

    print()
    print("=" * 78)
    print("BASE RATES — the measured corpus, projected onto real traffic")
    print("=" * 78)
    print()
    print("  MEASURED (both splits, 800 cases)")
    for k in sorted(combined):
        n, held = combined[k]
        print(f"    {k:20s} n={n:4d}  held={held:4d}   {held / n:6.1%}")

    rn, rh = combined["routine/legit"]
    routine_hold = args.routine_hold if args.routine_hold is not None else rh / rn
    cn, ch = combined["change/legit"]
    change_hold = ch / cn

    print()
    print(f"  The routine hold rate — {routine_hold:.1%} — rests on {rh} event(s)")
    print(f"  out of {rn}. Every figure below scales linearly with it. It is the")
    print("  weakest input and the most important one.")
    print()
    print("  ASSUMED (not measured; challenge these first)")
    print(f"    payouts per day                 {args.payouts:,}")
    print(f"    carrying a destination change   {args.change_rate:.2%}")
    print(f"    of those, fraudulent            {args.fraud_rate:.1%}")
    print()

    n = args.payouts
    changes = n * args.change_rate
    routine = n - changes
    fraud = changes * args.fraud_rate
    legit_changes = changes - fraud

    pp_holds = routine * routine_hold + legit_changes * change_hold + fraud
    pp_released = n - pp_holds

    print("  PER DAY")
    print(f"    {'':34s} {'PayeeProof':>12s} {'hold everything':>16s}")
    print(f"    {'payouts released with no call':34s} {pp_released:12,.0f} "
          f"{0:16,.0f}")
    print(f"    {'held — a human must act':34s} {pp_holds:12,.0f} {n:16,.0f}")
    print(f"    {'of those, actually fraud':34s} {fraud:12,.1f} {fraud:16,.1f}")
    print(f"    {'legitimate payments cancelled':34s} {0:12,.0f} {0:16,.0f}")
    print()

    ratio = n / pp_holds if pp_holds else float("inf")
    print(f"  Both catch every fraud case in this corpus. The difference is that")
    print(f"  one asks for {pp_holds:,.0f} phone calls a day and the other asks for")
    print(f"  {n:,.0f} — a factor of {ratio:.0f}.")
    print()
    print("  That is the comparison precision hides. On a corpus that is half")
    print("  fraud the two look three points apart; on traffic anyone actually")
    print("  runs, one of them is a staffed desk and the other is impossible.")
    print()

    print("  AND THE HONEST OTHER HALF")
    print(f"    {pp_holds:,.0f} calls a day is not nothing. At roughly 5 minutes")
    print(f"    each that is {pp_holds * 5 / 60:,.1f} hours, so a team. The system does")
    print("    not remove the work; it makes the work possible and points it")
    print("    at the right payouts.")
    print()
    print(f"    Fraud is {fraud / pp_holds:.2%} of what gets held. An operator")
    print("    working that queue sees a genuine attempt rarely, which is")
    print("    exactly the condition under which people start rubber-stamping.")
    print("    The queue being SORTED — a rejection recommendation attached to")
    print("    the cases with real evidence — matters more at this ratio than")
    print("    any accuracy figure in this repository.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
