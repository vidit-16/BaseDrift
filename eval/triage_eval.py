"""
PayeeProof — triage evaluation.

Runs the funnel over a synthetic AP inbox and reports what it costs and what it
loses. No API key: the classifier stage falls back to its deterministic
pre-read, and that limitation is reported rather than hidden.

    python eval/triage_eval.py
    python eval/triage_eval.py --split holdout

THE NUMBER THIS FILE EXISTS FOR
===============================
Not the funnel counts. The allowlist counterfactual.

"Only read mail from senders in the vendor master" is the obvious first stage,
it removes almost all the noise, and it DELETES THE FRAUD — a typosquat is by
construction not in the master. This eval measures exactly how many genuine
change requests, and how many fraudulent ones, that rule would silently discard.
A triage stage that improves every operational metric while removing the threat
class is the kind of thing that only shows up if you go looking for it.
"""

import argparse
import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import pipeline  # noqa: E402
import triage as T  # noqa: E402

DATA = os.path.join(HERE, "..", "data")


def load_inbox(split):
    path = os.path.join(DATA, f"inbox_{split}.csv")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found. Run: python data/generate_inbox.py")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_case_labels(split):
    path = os.path.join(DATA, f"cases_{split}.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


def as_message(row):
    return T.Message(
        message_id=row["message_id"],
        from_addr=row["from_addr"],
        subject=row["subject"],
        body=row["body"],
        thread_id=row["thread_id"],
        to_addr=row["to_addr"],
        received_at=float(row["received_at"]),
        headers=json.loads(row["headers"] or "{}"),
        in_reply_to=row["in_reply_to"],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    args = ap.parse_args()

    rows = load_inbox(args.split)
    cases = load_case_labels(args.split)
    vendors = pipeline.load_vendors()

    seen = set()
    results = []
    for row in rows:
        results.append(T.triage(as_message(row), vendors, seen, classifier=None))

    n = len(rows)
    changes = [r for r, row in zip(results, rows) if row["is_change_request"] == "True"]
    noise = [r for r, row in zip(results, rows) if row["is_change_request"] != "True"]

    print()
    print("=" * 78)
    print(f"TRIAGE EVAL — {args.split} inbox, {n} messages, "
          f"{len(changes)} genuine change requests ({len(changes) / n:.1%})")
    print("=" * 78)
    print("No API key used. The classifier stage falls back to its deterministic")
    print("pre-read, so the CLASSIFY numbers below are a floor, not the design.")
    print()

    funnel = T.funnel_summary(results)
    print("  the funnel, cheapest stage first")
    print(f"    {'verdict':16s} {'all':>7s} {'changes':>9s} {'noise':>7s}")
    for verdict in (T.DUPLICATE, T.DROPPED, T.UNKNOWN, T.NOT_A_CHANGE, T.ROUTE):
        c = sum(1 for r in changes if r.verdict == verdict)
        d = sum(1 for r in noise if r.verdict == verdict)
        print(f"    {verdict:16s} {funnel[verdict]:7d} {c:9d} {d:7d}")
    print()

    routed = sum(1 for r in results if r.routed)
    kept = sum(1 for r in changes if r.routed)
    print(f"  {routed} of {n} messages reach the decision pipeline "
          f"({routed / n:.1%}).")
    print(f"  {kept} of {len(changes)} genuine change requests survive "
          f"({kept / len(changes):.1%}).")
    lost = [r for r in changes if not r.routed]
    if lost:
        by_stage = collections.Counter(r.stage for r in lost)
        print(f"  {len(lost)} LOST, by stage: {dict(by_stage)}")
    print()

    # ── Vendor resolution ────────────────────────────────────────────
    exact = sum(1 for r in results if r.match == "exact")
    look = sum(1 for r in results if r.match == "lookalike")
    content = sum(1 for r in results if r.match == "content")
    ambiguous = sum(1 for r in results if len(r.candidates) > 1)
    wrong = sum(1 for r, row in zip(results, rows)
                if r.vendor_id and row["true_vendor_id"]
                and r.vendor_id != row["true_vendor_id"]
                and r.match == "exact")
    print(f"  vendor resolution: {exact} exact, {look} lookalike, {content} by "
          f"content, {ambiguous} ambiguous")
    print(f"                     {wrong} exact matches disagreeing with the true "
          f"sender")
    ch_content = sum(1 for r in changes if r.match == "content")
    no_content = sum(1 for r in noise if r.match == "content")
    print(f"  content match rescued {ch_content} change request(s) that no domain "
          f"rule would reach,")
    print(f"  and pulled in {no_content} noise message(s).")
    print()

    # ── THE COUNTERFACTUAL ───────────────────────────────────────────
    print("  " + "-" * 74)
    print("  WHAT AN ALLOWLIST WOULD HAVE DONE")
    print("  " + "-" * 74)
    print("  \"only read mail from domains in the vendor master\" — the obvious")
    print("  first stage, and the one that removes the fraud.")
    print()

    # An allowlist keeps ONLY exact domain matches. Lookalikes and content
    # matches both fall outside it, so both are counted here.
    dropped_by_allowlist = [(r, row) for r, row in zip(results, rows)
                            if row["is_change_request"] == "True"
                            and r.match != "exact"]
    fraud_dropped = [row for _, row in dropped_by_allowlist
                     if cases.get(row["case_id"], {}).get("label") == "fraud"]
    legit_dropped = [row for _, row in dropped_by_allowlist
                     if cases.get(row["case_id"], {}).get("label") == "legit"]

    total_fraud = sum(1 for row in rows
                      if row["is_change_request"] == "True"
                      and cases.get(row["case_id"], {}).get("label") == "fraud")

    print(f"    change requests it would drop     {len(dropped_by_allowlist)}")
    print(f"      of which FRAUDULENT             {len(fraud_dropped)}  "
          f"({len(fraud_dropped) / total_fraud:.1%} of all fraud in the inbox)")
    print(f"      of which legitimate             {len(legit_dropped)}")
    print()
    by_scenario = collections.Counter(
        cases[row["case_id"]]["scenario_type"] for row in fraud_dropped)
    for k, v in sorted(by_scenario.items()):
        print(f"      {k:28s} {v}")
    print()
    print("  Two populations, and an allowlist cannot tell them apart because")
    print("  both are simply \"a sender that is not in the master\":")
    print("    - the fraud is typosquats and forged domains. Not being in the")
    print("      master is what makes them what they are.")
    print("    - the legitimate ones are acquired vendors writing from the new")
    print("      parent's domain.")
    print("  So the stage is \"in the master, OR a lookalike of one, OR quoting an")
    print("  identifier that is\" — and the match KIND travels onward as evidence")
    print("  rather than being consumed here.")
    print()

    # ── Cost ─────────────────────────────────────────────────────────
    cheap = sum(1 for r in results if r.stage in (T.S_DEDUPE, T.S_INGEST, T.S_VENDOR))
    print("  " + "-" * 74)
    print("  COST")
    print("  " + "-" * 74)
    print(f"    resolved with NO model call       {cheap} ({cheap / n:.1%})")
    print(f"    reaching the classifier            {n - cheap}")
    print(f"    reaching extraction                {routed}")
    print()
    print(f"  Extracting every message would be {n} calls. The funnel makes it")
    print(f"  {routed} — a {1 - routed / n:.1%} reduction, and the stage doing")
    print(f"  most of that work runs no model at all.")
    print()
    print("  CAVEAT. The noise here is authored, and authored noise is easier")
    print("  than real mail: no forwarded chains, no attachments-only messages,")
    print("  no vendors who write like spammers. Treat the reduction as an upper")
    print("  bound on a corpus built by the same people who built the funnel.")
    print()


if __name__ == "__main__":
    main()
