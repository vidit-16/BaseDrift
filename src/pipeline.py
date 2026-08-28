"""
PayeeProof — pipeline.py

One function that runs a case end to end and returns a full audit record.

    email text
        -> semantic extraction        (LLM — evidence only)
        -> FAV replay                 (schema-faithful)
        -> deterministic decision     (no LLM)
        -> callback verification      (if STEP_UP)
        -> RazorpayX API actions
        -> audit trail

Run directly for a demo:
    python pipeline.py
"""

import csv
import json
import os
from dataclasses import asdict
from typing import Optional, Dict, Any

import extractor
import llm_client
from extractor import ExtractionResult
from decision_engine import (
    decide, Decision, FAVResult, VendorRecord,
    ALLOW, STEP_UP, BLOCK,
)
import verifier


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ── Vendor master loading ─────────────────────────────────────────────

def load_vendors(path: Optional[str] = None) -> Dict[str, VendorRecord]:
    path = path or os.path.join(DATA_DIR, "vendor_master.csv")
    vendors = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vendors[row["vendor_id"]] = VendorRecord(
                vendor_id=row["vendor_id"],
                legal_name=row["legal_name"],
                gstin=row["gstin"],
                known_domain=row["known_domain"],
                known_phone=row["known_phone"],
                known_account_number=row["known_account_number"],
                known_ifsc=row["known_ifsc"],
                avg_payout_amount=float(row["avg_payout_amount"]),
            )
    return vendors


def build_account_index(vendors: Dict[str, VendorRecord]) -> Dict[str, str]:
    """
    {account_number: vendor_id} across the whole master.
    Enables the cross-contact reuse check without a graph database.
    """
    idx = {}
    for v in vendors.values():
        for acct in v.all_known_accounts():
            idx[acct] = v.vendor_id
    return idx


# ── Main entry point ──────────────────────────────────────────────────

def run_case(email_text: str,
             vendor: VendorRecord,
             fav: FAVResult,
             callback_reaches_known_contact: bool = False,
             account_index: Optional[Dict[str, str]] = None,
             near_duplicate: bool = False,
             split_below: bool = False,
             case_id: str = "CASE",
             payout_id: str = "pout_TEST",
             fund_account_id: str = "fa_TEST",
             destination_account_number: Optional[str] = None,
             controls_existing_account: Optional[bool] = None,
             ground_truth: Optional[str] = None) -> Dict[str, Any]:
    """
    Full pipeline for one case. Never raises.
    Returns a dict that IS the audit record.

    destination_account_number is the account the payout will actually credit.
    In production it is resolved from the payout's fund_account_id at the
    payout.pending webhook, before any money moves. It is the authoritative
    input: without it the engine can only inspect the account number the
    message claimed, which is a self-reported value. Leave it None only for
    offline analysis of raw messages, where the audit record will say so.
    """

    # 1. Semantic extraction — the only LLM step
    ext = extractor.extract(email_text)

    # 2. Deterministic decision
    dec = decide(ext, fav, vendor,
                 other_vendor_accounts=account_index,
                 near_duplicate=near_duplicate,
                 split_below=split_below,
                 destination_account_number=destination_account_number)

    # 3. Verification, only if the decision asked for it
    ver = verifier.verify(dec, vendor, callback_reaches_known_contact, case_id,
                          controls_existing_account=controls_existing_account)

    final_outcome  = ver.final_outcome if ver else dec.outcome
    payout_allowed = ver.payout_allowed if ver else dec.payout_allowed

    # 4. What RazorpayX calls this maps to
    actions = verifier.razorpay_actions(
        final_outcome,
        ver.reason if ver else dec.reason,
        payout_id, fund_account_id,
    )

    # 5. Audit record
    audit = {
        "case_id": case_id,
        "vendor_id": vendor.vendor_id,
        "ground_truth": ground_truth,

        "payout": {
            "payout_id": payout_id,
            "fund_account_id": fund_account_id,
            "destination_account_number": destination_account_number,
            "destination_known": destination_account_number is not None,
        },

        "extraction": ext.to_dict(),

        "fav_result": {
            "account_status": fav.account_status,
            "registered_name": fav.registered_name,
            "name_match_score": fav.name_match_score,
            "note": "schema-faithful replay — FAV is unavailable in test mode",
        },

        "decision": dec.to_dict(),
        "verification": ver.to_dict() if ver else None,

        "final_outcome": final_outcome,
        "payout_allowed": payout_allowed,
        "razorpay_actions": actions,
    }

    # Scoring. An extraction failure routes to STEP_UP under R1, which is the
    # correct POLICY — inconclusive is not clean. But it is NOT a detection.
    # Grading it as one lets a dead API key, a rate limit, or a deprecated model
    # id read as "fraud caught", inflating recall on any bulk eval run. Keep
    # "we could not evaluate this" distinguishable from "we found something".
    if ground_truth:
        if not ext.ok:
            audit["scored"] = False
            audit["scoring_status"] = "inconclusive_extraction_failed"
            audit["correct"] = None
        else:
            caught = final_outcome in (BLOCK, STEP_UP)
            audit["scored"] = True
            audit["scoring_status"] = "scored"
            audit["correct"] = caught if ground_truth == "fraud" else (not caught)

    return audit


def summarize(audit: Dict[str, Any]) -> str:
    """Readable one-screen summary for demos."""
    L = []
    L.append(f"case {audit['case_id']}  vendor {audit['vendor_id']}")

    ext = audit["extraction"]
    if ext["ok"]:
        s = ext["semantic"]
        L.append(f"  semantic : {s['intent']} / {s['action']} / {s['scope']}")
        if s.get("reasoning"):
            L.append(f"             {s['reasoning']}")
    else:
        L.append(f"  semantic : FAILED — {ext['failure_reason']}")

    fav = audit["fav_result"]
    L.append(f"  FAV      : status={fav['account_status']} "
             f"name={fav['registered_name']} score={fav['name_match_score']}")

    d = audit["decision"]
    if d.get("checked_destination"):
        src = d.get("destination_source")
        note = ("payout fund account" if src == "razorpay_payout"
                else "UNVERIFIED — self-reported in the request")
        L.append(f"  dest     : {d['checked_destination']}  ({note})")
    # INCONCLUSIVE is 12 chars; keep the columns aligned so the signal table
    # stays readable in the demo.
    for tier in ("tier1", "tier2"):
        tag = "T1" if tier == "tier1" else "T2"
        for s in d.get(tier, []):
            L.append(f"  {tag} {s['result']:12s} {s['name']:20s} {s['detail']}")

    L.append(f"  decision : {d['outcome']}  [{d['rule_fired']}]")
    L.append(f"             {d['reason']}")

    v = audit.get("verification")
    if v:
        L.append(f"  callback : {v['outcome']} to {v['contact_used']} "
                 f"(source: {v['contact_source']})")
        L.append(f"             {v['reason']}")

    L.append(f"  FINAL    : {audit['final_outcome']}   payout_allowed={audit['payout_allowed']}")
    for a in audit["razorpay_actions"]:
        if a["method"]:
            L.append(f"  api      : {a['method']} {a['endpoint']} -> {a['effect']}")
        else:
            L.append(f"  api      : {a['effect']}")

    if audit.get("ground_truth"):
        if audit.get("correct") is None:
            mark = "NOT SCORED — extraction never ran"
        else:
            mark = "CORRECT" if audit["correct"] else "INCORRECT"
        L.append(f"  truth    : {audit['ground_truth'].upper()} -> {mark}")

    return "\n".join(L)


# ── Demo ──────────────────────────────────────────────────────────────

HERO_EMAIL = """
From: payments@balaj1logistic.com
To: accounts@clientcorp.in

Hi Meera,

Our treasury has consolidated everything into a single facility this
quarter. INV-4471 from October is still open on our ledger, and the
retainer continues through March. Everything should reach 351349409853,
KKBK0238196 from here.

Our GST registration should be the same as before, 07JQQPG8009O1Z2.

Month-end closing is tomorrow, so do prioritise this one. Please reply
on this thread rather than the old chain.

Priya Nair
Accounts, Balaji Logistics
""".strip()


def demo():
    # Refuse to run without a key. Without one, extraction fails, R1 holds the
    # payout, and the hold superficially resembles a catch. Printing a verdict
    # here would claim the semantic layer worked when it never ran at all.
    if not llm_client.get_api_key():
        print()
        print("GROQ_API_KEY is not set — the semantic layer cannot run.")
        print()
        print("This demo deliberately refuses to print a verdict without it.")
        print("A payout held because extraction failed is not a detection, and")
        print("presenting it as one would misrepresent what PayeeProof does.")
        print()
        print('  PowerShell:  $env:GROQ_API_KEY="gsk_..."')
        print("  cmd:         set GROQ_API_KEY=gsk_...")
        print()
        print("Free key, no credit card: console.groq.com")
        print()
        raise SystemExit(1)

    print()
    print("=" * 72)
    print("PayeeProof — hero case")
    print("Every bank-level check passes. Change authorization is absent.")
    print("=" * 72)
    print()

    vendor = VendorRecord(
        vendor_id="VEND0069",
        legal_name="Balaji Logistics",
        gstin="07JQQPG8009O1Z2",
        known_domain="balajilogistic.com",
        known_phone="9088190947",
        known_account_number="434392416664",
        known_ifsc="KKBK0403467",
        avg_payout_amount=28000.0,
    )

    # FAV passes cleanly: the attacker put the real vendor name on an
    # account they control, so the bank returns a near-perfect match.
    fav = FAVResult(
        account_status="active",
        registered_name="Balaji Logistics",
        name_match_score=99,
    )

    audit = run_case(
        HERO_EMAIL, vendor, fav,
        callback_reaches_known_contact=False,   # attacker has no access to it
        case_id="HERO",
        # A fund account was created from the spoofed email and a payout is
        # pending to it. In production this is read back from the payout's
        # fund_account_id at payout.pending — it is the account the money
        # would actually credit, not the number the email happened to state.
        destination_account_number="351349409853",
        ground_truth="fraud",
    )

    print(summarize(audit))
    print()
    print("Full audit record:")
    print(json.dumps(audit, indent=2)[:1500] + "\n...")


if __name__ == "__main__":
    demo()
