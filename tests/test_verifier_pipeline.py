"""
PayeeProof — verifier and pipeline tests.

The verifier holds the project's central invariant: the callback goes to the
number already on file, never to one supplied in the request. An attacker who
puts their own phone number in a spoofed email must not be the one who answers.

The pipeline tests cover scoring, which has its own failure mode: an extraction
failure routes to STEP_UP under R1, and grading that as a detection would let a
dead API key read as "fraud caught".

No API key, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pipeline  # noqa: E402
import verifier  # noqa: E402
from decision_engine import (  # noqa: E402
    ALLOW, BLOCK, STEP_UP, Decision, FAVResult, VendorRecord,
)
from extractor import ExtractionResult  # noqa: E402

KNOWN_PHONE = "9088190947"
ATTACKER_PHONE = "9999900000"

VENDOR = VendorRecord(
    vendor_id="VEND0069", legal_name="Balaji Logistics",
    gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
    known_phone=KNOWN_PHONE, known_account_number="434392416664",
    known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
)


def dec(outcome, rule="R_TEST", allowed=False):
    return Decision(outcome=outcome, rule_fired=rule, reason="because",
                    triggered_by=[], payout_allowed=allowed)


# ══ verifier: the callback invariant ══════════════════════════════════

def test_callback_uses_the_number_on_file():
    r = verifier.verify(dec(STEP_UP), VENDOR, True, "C1")
    assert r.contact_used == KNOWN_PHONE
    assert r.contact_source == "vendor_master"


def test_callback_never_uses_a_number_from_the_request():
    """
    Structural, and deliberately so: verify() has no parameter through which a
    request-supplied number could reach it. This test fails the moment someone
    adds one.
    """
    import inspect
    params = set(inspect.signature(verifier.verify).parameters)
    assert params == {"decision", "vendor", "callback_reaches_known_contact",
                      "case_id"}, params

    # And with an attacker's number all over the extraction, the callback is
    # still placed to the vendor master's number.
    ext = ExtractionResult(ok=True, sender_phone=ATTACKER_PHONE)
    assert ext.sender_phone == ATTACKER_PHONE
    r = verifier.verify(dec(STEP_UP), VENDOR, False, "C1")
    assert r.contact_used == KNOWN_PHONE
    assert ATTACKER_PHONE not in r.reason


def test_only_step_up_routes_to_verification():
    assert verifier.verify(dec(ALLOW, allowed=True), VENDOR, True, "C") is None
    assert verifier.verify(dec(BLOCK), VENDOR, True, "C") is None
    assert verifier.verify(dec(STEP_UP), VENDOR, True, "C") is not None


def test_reached_vendor_confirms_and_releases():
    r = verifier.verify(dec(STEP_UP), VENDOR, True, "C1")
    assert r.outcome == verifier.CONFIRMED
    assert r.final_outcome == ALLOW
    assert r.payout_allowed is True
    assert r.escalated is False


def test_unreachable_vendor_holds_and_escalates():
    """The hold is never auto-released — that is the whole point of the rule."""
    r = verifier.verify(dec(STEP_UP), VENDOR, False, "C1")
    assert r.outcome == verifier.UNREACHABLE
    assert r.final_outcome == STEP_UP
    assert r.payout_allowed is False
    assert r.escalated is True
    assert r.attempts == verifier.MAX_CALLBACK_ATTEMPTS


def test_hold_is_bounded():
    """Razorpay auto-rejects after ~3 months; escalation must come long before."""
    assert 1 <= verifier.MAX_CALLBACK_ATTEMPTS <= 5


def test_simulation_is_labelled_as_simulation():
    for reached in (True, False):
        r = verifier.verify(dec(STEP_UP), VENDOR, reached, "C1")
        assert r.simulated is True
        assert r.simulation_basis


def test_verification_ids_are_unique():
    ids = {verifier.verify(dec(STEP_UP), VENDOR, True, "C1").verification_id
           for _ in range(20)}
    assert len(ids) == 20


# ══ verifier: the API actions a decision maps to ══════════════════════

def test_allow_maps_to_approve():
    acts = verifier.razorpay_actions(ALLOW, "clean", "pout_1", "fa_1")
    assert len(acts) == 1
    assert acts[0]["method"] == "POST"
    assert acts[0]["endpoint"] == "/v1/payouts/pout_1/approve"


def test_block_rejects_and_deactivates_the_fund_account():
    acts = verifier.razorpay_actions(BLOCK, "bad", "pout_1", "fa_1")
    assert [a["method"] for a in acts] == ["POST", "PATCH"]
    assert acts[0]["endpoint"] == "/v1/payouts/pout_1/reject"
    assert acts[1]["endpoint"] == "/v1/fund_accounts/fa_1"
    assert acts[1]["body"] == {"active": False}


def test_step_up_makes_no_api_call():
    """Inaction is the safe state: the payout simply stays pending."""
    acts = verifier.razorpay_actions(STEP_UP, "checking", "pout_1", "fa_1")
    assert all(a["method"] is None for a in acts)


def test_remarks_are_bounded():
    acts = verifier.razorpay_actions(ALLOW, "x" * 5000, "p", "f")
    assert len(acts[0]["body"]["remarks"]) < 250


# ══ pipeline: scoring must not credit a failure as a detection ════════

def _run(ext_ok, ground_truth, dest="434392416664", reached=False):
    import extractor as E
    real = E.extract
    E.extract = lambda *a, **k: (
        ExtractionResult(ok=True, intent="PAYMENT_FOLLOWUP", action="NONE",
                         scope="NONE")
        if ext_ok else
        ExtractionResult(ok=False, failure_reason="API down"))
    try:
        return pipeline.run_case(
            "doc", VENDOR, FAVResult("active", "Balaji Logistics", 99),
            callback_reaches_known_contact=reached,
            destination_account_number=dest,
            case_id="T", ground_truth=ground_truth)
    finally:
        E.extract = real


def test_extraction_failure_is_not_scored_as_a_catch():
    """
    R1 holding the payout is correct policy but is NOT a detection. Grading it
    as one lets a dead key or a rate limit read as fraud caught, inflating
    recall on any bulk run.
    """
    a = _run(ext_ok=False, ground_truth="fraud")
    assert a["decision"]["rule_fired"] == "R1_extraction_failed"
    assert a["correct"] is None
    assert a["scored"] is False
    assert a["scoring_status"] == "inconclusive_extraction_failed"


def test_successful_extraction_is_scored():
    a = _run(ext_ok=True, ground_truth="legit")
    assert a["scored"] is True
    assert a["correct"] is True          # known destination, legit -> allowed


def test_summarize_never_prints_a_verdict_for_an_unscored_case():
    text = pipeline.summarize(_run(ext_ok=False, ground_truth="fraud"))
    assert "NOT SCORED" in text
    assert "-> CORRECT" not in text


def test_audit_records_the_destination_and_its_provenance():
    a = _run(ext_ok=True, ground_truth="legit", dest="434392416664")
    assert a["payout"]["destination_account_number"] == "434392416664"
    assert a["decision"]["destination_source"] == "razorpay_payout"


def test_run_case_without_ground_truth_omits_scoring_keys():
    import extractor as E
    real = E.extract
    E.extract = lambda *a, **k: ExtractionResult(
        ok=True, intent="PAYMENT_FOLLOWUP", action="NONE", scope="NONE")
    try:
        a = pipeline.run_case("doc", VENDOR,
                              FAVResult("active", "Balaji Logistics", 99),
                              destination_account_number="434392416664")
    finally:
        E.extract = real
    assert "correct" not in a


# ══ pipeline: vendor master loading ═══════════════════════════════════

def test_vendor_master_loads_and_indexes():
    vendors = pipeline.load_vendors()
    assert len(vendors) == 120
    idx = pipeline.build_account_index(vendors)
    assert len(idx) == 120
    for acct, vid in list(idx.items())[:5]:
        assert vendors[vid].known_account_number == acct


def test_data_dir_resolves_inside_the_repo():
    """It used to point one level above the repo root and find nothing."""
    resolved = os.path.abspath(pipeline.DATA_DIR)
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    assert resolved.startswith(repo)


# ══ Runner ════════════════════════════════════════════════════════════

def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n        {e or 'assertion failed'}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print()
    print(f"  {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
