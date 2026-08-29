"""
PayeeProof — end-to-end trace of a single case.

Shows every step from raw email to RazorpayX call, and labels each one as either
a MODEL step or a DETERMINISTIC step.

The thing worth seeing: there is exactly ONE model call, at the start, and it
produces evidence. Nothing after it consults a model — not the signals, not the
rule that fires, not the verification, not the action. The model is never asked
to reason about its own output, never asked whether something looks fraudulent,
and never given a tool it could act with.

That is not a limitation to apologise for. A model that can reach the approve
endpoint is the thing you do not want in a payment control. The value is that
the model handles the unbounded input — natural language, infinitely variable —
while the decision stays bounded, auditable, and reproducible from its record.

    python eval/trace_case.py                 # one fraud, one legitimate
    python eval/trace_case.py CASE00042       # a specific case

Uses cached extractions where available so a trace costs no API calls.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "data"))
sys.path.insert(0, HERE)

import extraction_eval as X  # noqa: E402
import render as R  # noqa: E402
import verifier  # noqa: E402
import decision_engine as DE  # noqa: E402
from decision_engine import FAVResult, decide  # noqa: E402
from rules_eval import load_cases, load_vendors  # noqa: E402

MODEL = "openai/gpt-oss-120b"
W = 78


def rule(char="─"):
    print(char * W)


def step(n, title, kind):
    tag = {"model": "[ MODEL CALL ]",
           "det": "[ DETERMINISTIC — no model ]",
           "io": "[ INPUT ]"}[kind]
    print()
    rule("═")
    print(f"STEP {n} — {title}")
    print(f"         {tag}")
    rule("═")


def trace(case_id, row, vendor, rendered, extraction):
    print()
    rule("█")
    print(f"  TRACE  {case_id}   scenario: {row['scenario_type']}   "
          f"truth: {row['label'].upper()}")
    rule("█")

    calls = 0

    # ── 1 ────────────────────────────────────────────────────────────
    step(1, "The two inputs", "io")
    print("  From RazorpayX (payout.pending):")
    print(f"    fund account       {row['case_id']}-fa")
    print(f"    destination        {row['proposed_account_number']}  <- authoritative")
    print(f"    amount             Rs {float(row['amount']):,.2f}")
    print()
    print("  From the merchant's AP inbox (the change request):")
    for line in rendered.email.splitlines()[:11]:
        print(f"    | {line}")
    print(f"    ... ({len(rendered.email)} chars, sha {rendered.sha256[:12]})")

    # ── 2 ────────────────────────────────────────────────────────────
    step(2, "Semantic extraction — the ONLY model call", "model")
    calls += 1
    s = extraction["semantic"]
    c = extraction["claims"]
    p = extraction["pressure"]
    print(f"  model        {extraction.get('_meta', {}).get('model', MODEL)}")
    print(f"  prompt hash  {extraction.get('_meta', {}).get('prompt_hash', '?')}")
    print(f"  sanitised    {extraction['hidden_chars_removed']} hidden chars removed, "
          f"truncated={extraction['document_truncated']}")
    print()
    print("  The model returns EVIDENCE. It is asked what the message MEANS —")
    print("  never whether it is fraudulent, and never what to do about it.")
    print()
    print(f"    intent   {s['intent']}")
    print(f"    action   {s['action']}")
    print(f"    scope    {s['scope']}")
    print(f"    why      {s['reasoning']}")
    print()
    print("  claims it read out (NEVER trusted as identity):")
    for k in ("proposed_account_number", "proposed_ifsc", "proposed_gstin",
              "sender_domain", "amount"):
        print(f"    {k:26s} {c[k]}")
    print()
    print("  pressure signals:")
    print(f"    urgency                    {p['urgency_detected']}  {p['urgency_phrases'][:1]}")
    print(f"    channel_manipulation       {p['channel_manipulation_detected']}")
    print(f"    hedging                    {p['hedging_detected']}  {p['hedged_fields'][:2]}")

    # ── 3 ────────────────────────────────────────────────────────────
    step(3, "Resolve the destination", "det")
    ext = X.result_from_dict(extraction)
    dest, src = DE.resolve_destination(ext, row["proposed_account_number"])
    print("  A plain lookup, no model. The payout's own fund account wins over")
    print("  anything the message claimed — that is the whole P0.3 property.")
    print()
    print(f"    model said the account is   {c['proposed_account_number']}")
    print(f"    the payout actually goes to {dest}")
    print(f"    source                      {src}")
    print(f"    -> checking                 {dest}")

    # ── 4 ────────────────────────────────────────────────────────────
    step(4, "Signals — every one a comparison, none a judgement", "det")
    avail = row["fav_name_available"] == "True"
    fav = FAVResult(row["fav_account_status"],
                    row["registered_name_returned"] if avail else None,
                    int(row["name_match_score"]) if avail else None)
    d = decide(ext, fav, vendor, other_vendor_accounts=index,
               near_duplicate=row["near_duplicate_invoice"] == "True",
               split_below=row["split_below_threshold"] == "True",
               destination_account_number=row["proposed_account_number"])

    for tier, label in ((d.tier1, "TIER 1 — identity, against the vendor master"),
                        (d.tier2, "TIER 2 — context, never decisive alone")):
        if not tier:
            continue
        print(f"  {label}")
        for sig in tier:
            flag = "  <- DECEPTION" if sig.deception else ""
            print(f"    {sig.result:13s} {sig.name:22s} {sig.detail[:44]}{flag}")
            print(f"    {'':13s} {'':22s} source: {sig.source}")
        print()

    # ── 5 ────────────────────────────────────────────────────────────
    step(5, "Rule engine — first match wins", "det")
    print("  Seven rules, evaluated in order. No model, no score, no threshold")
    print("  that anyone has to take on trust. The table is in the docstring and")
    print("  the code implements exactly it.")
    print()
    for r in ("R1 extraction failed", "R2 no change requested",
              "R3 any Tier 1 FAIL", "R4 REPLACE + new account + deception",
              "R5 any Tier 1 not-clean", "R6 any Tier 2 not-clean",
              "R7 all clear"):
        # startswith, not `in`: "r1" is a substring of "R5_tier1_inconclusive",
        # so a containment test marked two rules as fired.
        hit = d.rule_fired.lower().startswith(r.split()[0].lower())
        print(f"    {'>>>' if hit else '   '} {r}{'   <- FIRED' if hit else ''}")
    print()
    print(f"  {d.rule_fired}  ->  {d.outcome}")
    print(f"  {d.reason[:200]}")

    # ── 6 ────────────────────────────────────────────────────────────
    step(6, "Verification — two channels, if the decision asked for it", "det")
    ver = verifier.verify(d, vendor, row["callback_reaches_known_contact"] == "True",
                          case_id,
                          controls_existing_account=row["controls_existing_account"] == "True")
    if ver is None:
        print("  Not reached — only a hold routes here.")
    else:
        print(f"    channel 1  callback to {vendor.known_phone} (vendor master) "
              f"-> {row['callback_reaches_known_contact']}")
        print(f"    channel 2  penny drop from {vendor.known_account_number} "
              f"-> {row['controls_existing_account']}")
        print()
        print(f"  {ver.outcome}  ->  {ver.final_outcome}")
        print(f"  {ver.reason[:200]}")

    final = ver.final_outcome if ver else d.outcome
    allowed = ver.payout_allowed if ver else d.payout_allowed

    # ── 7 ────────────────────────────────────────────────────────────
    step(7, "What it maps to at RazorpayX", "det")
    for a in verifier.razorpay_actions(final, (ver.reason if ver else d.reason),
                                       f"pout_{case_id}", f"fa_{case_id}"):
        if a["method"]:
            extra = "   [needs human confirmation]" if a.get("requires_human_confirmation") else ""
            print(f"    {a['method']:6s} {a['endpoint']}{extra}")
        else:
            print(f"    (no call — {a['effect']})")

    print()
    rule("═")
    print(f"  OUTCOME {final}   payout_allowed={allowed}   truth={row['label'].upper()}")
    print(f"  MODEL CALLS USED: {calls}   —   steps 3 through 7 used none.")
    rule("═")


if __name__ == "__main__":
    vendors, index = load_vendors()
    rows = {r["case_id"]: r for r in load_cases("dev")}
    rendered = {c.case_id: c for c in R.render_split("dev")}

    wanted = sys.argv[1:]
    if not wanted:
        # One of each, both with a cached real extraction so this costs nothing.
        picked = {"fraud": None, "legit": None}
        for cid, row in rows.items():
            if picked[row["label"]] is None and os.path.exists(
                    X.cache_path(rendered[cid].sha256, MODEL, 1)):
                picked[row["label"]] = cid
            if all(picked.values()):
                break
        wanted = [picked["fraud"], picked["legit"]]

    for cid in wanted:
        path = X.cache_path(rendered[cid].sha256, MODEL, 1)
        if not os.path.exists(path):
            raise SystemExit(f"{cid} has no cached extraction; run "
                             f"eval/extraction_eval.py first (costs API calls).")
        with open(path, encoding="utf-8") as f:
            trace(cid, rows[cid], vendors[rows[cid]["vendor_id"]],
                  rendered[cid], json.load(f))
