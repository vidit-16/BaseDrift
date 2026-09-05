"""
BaseDrift — verifier and pipeline tests.

The verifier holds the project's central invariant: the callback goes to the
number already on file, never to one supplied in the request. An attacker who
puts their own phone number in a spoofed email must not be the one who answers.

The pipeline tests cover scoring, which has its own failure mode: an extraction
failure routes to STEP_UP under R1, and grading that as a detection would let a
dead API key read as "fraud caught".

No API key, no network.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pipeline  # noqa: E402
import verifier  # noqa: E402
from decision_engine import (  # noqa: E402
    ALLOW, BLOCK, STEP_UP, Decision, FAVResult, VendorRecord, AccountRecord,
)
from extractor import ExtractionResult  # noqa: E402

KNOWN_PHONE = "9088190947"
ATTACKER_PHONE = "9999900000"

KNOWN_ACCT = "434392416664"
AS_OF = "2026-06-30"

# The account the system will name: settled, long-standing, and added through
# onboarding rather than through the channel being verified.
ANCHOR = AccountRecord(
    account_number=KNOWN_ACCT, ifsc="KKBK0403467", status="active",
    added_on="2023-02-14", added_via="onboarding", verified_by="onboarding_kyc",
    verified_on="2023-02-14", settled_payout_count=14,
    last_settled_on="2026-05-30", is_primary=True,
)

VENDOR = VendorRecord(
    vendor_id="VEND0069", legal_name="Balaji Logistics",
    gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
    known_phone=KNOWN_PHONE, known_account_number=KNOWN_ACCT,
    known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
    accounts=[ANCHOR],
)


def with_accounts(*accounts):
    """The same vendor with a different set of accounts on file."""
    import dataclasses
    return dataclasses.replace(VENDOR, accounts=list(accounts))


def chan2(vendor=VENDOR, callback=False, controls=(), rule="R_TEST",
          recommended_action=None, as_of=AS_OF, via="email_request"):
    return verifier.verify(dec(STEP_UP, rule, recommended_action=recommended_action),
                           vendor, callback, "C1",
                           requester_controls_accounts=list(controls),
                           as_of=as_of, requested_via=via)


def dec(outcome, rule="R_TEST", allowed=False, recommended_action=None):
    return Decision(outcome=outcome, rule_fired=rule, reason="because",
                    triggered_by=[], payout_allowed=allowed,
                    recommended_action=recommended_action)


# ══ verifier: the callback invariant ══════════════════════════════════

def test_callback_uses_the_number_on_file():
    r = verifier.verify(dec(STEP_UP), VENDOR, True, "C1")
    assert r.contact_used == KNOWN_PHONE
    assert r.contact_source == "vendor_master"


def test_every_verify_call_site_in_the_repo_uses_the_current_signature():
    """
    A whole-repo grep, because the test suite CANNOT reach some of these.

    eval/extraction_eval.py needs an API key, so it is excluded from
    run_all.py — and when verify() gained requester_controls_accounts, its call
    site there kept passing the removed controls_existing_account. Nothing
    failed until a re-extraction run had already spent its API budget and
    crashed at the scoring step.

    Signature changes are cheap to make and expensive to discover late. This
    finds them in under a second.
    """
    import inspect
    import pathlib

    valid = set(inspect.signature(verifier.verify).parameters)
    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"verify\((.*?)\)", re.S)
    removed = {"controls_existing_account"}

    checked = 0
    for path in sorted(root.rglob("*.py")):
        # Relative to the repo root, never the absolute path. Checking absolute
        # components meant the exclusion fired on any ANCESTOR directory that
        # happened to be called scratchpad — a checkout under one skipped every
        # file in the repo and the test passed by finding nothing, which this
        # assert then caught only because it counts what it checked.
        parts = path.relative_to(root).parts
        if ".git" in parts or "scratchpad" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "verifier.verify(" not in text:
            continue
        for call in pattern.findall(text):
            checked += 1
            for kw in re.findall(r"(\w+)\s*=", call):
                assert kw not in removed, (
                    f"{path.relative_to(root)} passes {kw!r} to verify(), "
                    f"which no longer exists")
                if kw in ("decision", "vendor") or not kw.islower():
                    continue
    assert checked >= 3, f"only found {checked} verify() call sites; grep broke"


def test_callback_never_uses_a_number_from_the_request():
    """
    Structural, and deliberately so: verify() has no parameter through which a
    request-supplied number could reach it. This test fails the moment someone
    adds one.
    """
    import inspect
    params = set(inspect.signature(verifier.verify).parameters)
    # Reviewed set. Adding a parameter FAILS this on purpose — each one has to
    # be checked for whether a request-supplied contact could arrive through it.
    # requester_controls_accounts is a list of account NUMBERS ALREADY ON FILE
    # — the requester cannot add one by writing it in an email. as_of is a date
    # and requested_via names a channel; neither can carry a contact. None is a
    # phone number or an address, which is the property this guards.
    # The old parameter was a boolean outcome of a penny drop, not a
    # contact, so it is safe. A parameter named anything like a phone number is
    # not.
    assert params == {"decision", "vendor", "callback_reaches_known_contact",
                      "case_id", "requester_controls_accounts", "as_of",
                      "requested_via"}, params
    assert not any(w in p.lower() for p in params
                   for w in ("phone", "contact_number", "sender", "email",
                             "reply_to")), params

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


# ══ The second channel ════════════════════════════════════════════════
# "Either channel passes" was the obvious rule and it is wrong: SIM-swap fraud
# has channel 1 PASSING, because the attacker holds the phone. Channel 2 is
# authoritative; channel 1 corroborates.

def test_penny_drop_from_the_named_account_releases():
    r = chan2(controls=[KNOWN_ACCT])
    assert r.outcome == verifier.CONTROL_PROVEN
    assert r.final_outcome == ALLOW
    assert r.payout_allowed is True
    # It tests the account THIS SYSTEM named, not anything from the request.
    assert r.verification_account == KNOWN_ACCT
    assert r.contact_used == KNOWN_ACCT


def test_a_confirmed_callback_cannot_overrule_a_failed_penny_drop():
    """
    THE sim-swap case. The attacker answers the phone, so channel 1 passes —
    and an either/or rule would release exactly the fraud this exists to stop.
    """
    r = chan2(callback=True, controls=[])
    assert r.payout_allowed is False
    assert r.final_outcome == STEP_UP
    assert r.outcome == verifier.CONTESTED
    assert r.escalated is True


def test_neither_channel_holds_and_escalates():
    r = chan2(callback=False, controls=[])
    assert r.outcome == verifier.UNREACHABLE
    assert r.payout_allowed is False
    assert r.escalated is True


def test_penny_drop_rescues_an_unreachable_vendor():
    """
    A genuine vendor who cannot answer the phone but still banks where they
    always have. Previously held for want of a phone call.
    """
    r = chan2(callback=False, controls=[KNOWN_ACCT])
    assert r.payout_allowed is True


def test_channel_two_not_attempted_falls_back_to_the_callback():
    """
    RPD is enabled on request, not by default. None means it was not attempted,
    and behaviour must be exactly what it was before the channel existed.
    """
    yes = verifier.verify(dec(STEP_UP), VENDOR, True, "C1",
                          requester_controls_accounts=None)
    no = verifier.verify(dec(STEP_UP), VENDOR, False, "C1",
                         requester_controls_accounts=None)
    assert yes.outcome == verifier.CONFIRMED and yes.payout_allowed is True
    assert no.outcome == verifier.UNREACHABLE and no.payout_allowed is False


def test_the_second_channel_never_creates_a_rejection():
    """
    It resolves holds; it must not manufacture blocks. A legitimate vendor whose
    old account is genuinely closed is held for a human, never rejected.
    """
    for callback in (True, False):
        r = chan2(callback=callback, controls=[])
        assert r.final_outcome != BLOCK


# ══ V2.6 — WHICH account the penny drop must come from ════════════════
# The premise channel 2 rests on is that the requester still controls where
# money has been going and an attacker cannot. With one account on file that is
# unambiguous. With several it is not, and the premise breaks SILENTLY.

def test_the_system_names_the_account_and_the_requester_never_chooses():
    named, basis = verifier.select_verification_account(VENDOR, AS_OF)
    assert named.account_number == KNOWN_ACCT
    assert "settled" in basis


def test_an_account_that_never_received_money_cannot_prove_anything():
    """Being listed proves nothing; money arriving proves control at that moment."""
    unpaid = AccountRecord(account_number="999900001111", added_on="2020-01-01",
                           added_via="portal", verified_by="penny_drop",
                           settled_payout_count=0, is_primary=True)
    named, _ = verifier.select_verification_account(with_accounts(unpaid), AS_OF)
    assert named is None


def test_a_recently_added_account_cannot_prove_anything():
    """An account added last week is exactly what a planted one looks like."""
    fresh = AccountRecord(account_number="999900002222", added_on="2026-06-01",
                          added_via="portal", verified_by="penny_drop",
                          settled_payout_count=5, is_primary=True)
    named, _ = verifier.select_verification_account(with_accounts(fresh), AS_OF)
    assert named is None


def test_an_account_added_by_email_cannot_verify_an_email_request():
    """
    The circularity. An attacker who once got an account added by email would
    otherwise use that success as the credential for the next request.
    """
    v = with_accounts(AccountRecord(
        account_number="999900003333", added_on="2023-01-01",
        added_via="email_request", verified_by="callback",
        settled_payout_count=9, is_primary=True))
    assert verifier.select_verification_account(v, AS_OF, "email_request")[0] is None
    # The same account is fine when a DIFFERENT channel is being verified.
    assert verifier.select_verification_account(v, AS_OF, "phone_request")[0] is not None


def test_the_oldest_qualifying_account_wins():
    """Age is the property being relied on, so the newest is the wrong pick."""
    newer = AccountRecord(account_number="999900004444", added_on="2025-01-01",
                          added_via="portal", verified_by="penny_drop",
                          settled_payout_count=3)
    named, _ = verifier.select_verification_account(
        with_accounts(newer, ANCHOR), AS_OF)
    assert named.account_number == KNOWN_ACCT


def test_a_planted_account_does_not_satisfy_the_named_account():
    """
    THE V2.6 ATTACK, end to end. The attacker got account B onto the master
    earlier — unverified, recent, never paid — and now asks for a change. Ask
    "prove you control an account on file" and they penny-drop from B, and the
    strongest control in the system confirms the fraud.

    The fix is that the system names the account. B is not it.
    """
    planted = AccountRecord(account_number="555566667777", added_on="2026-06-10",
                            added_via="email_request", verified_by="unverified",
                            settled_payout_count=0)
    v = with_accounts(ANCHOR, planted)
    r = chan2(vendor=v, callback=False, controls=[planted.account_number])
    assert r.payout_allowed is False
    assert r.verification_account == KNOWN_ACCT
    assert "not the same thing" in r.reason


def test_channel_two_unavailable_escalates_and_never_falls_back():
    """
    THE THIRD STATE, and the reason it is not simply "not attempted".

    A vendor with no qualifying account and a callback that PASSES. Treating
    unavailable as not-attempted would fall through to the callback and release
    this — and the callback is exactly what sim-swap defeats. The bug would look
    like an ordinary verified change in the audit.
    """
    v = with_accounts(AccountRecord(account_number="888800001111",
                                    added_on="2026-06-20",
                                    added_via="email_request",
                                    verified_by="unverified",
                                    settled_payout_count=0, is_primary=True))
    r = chan2(vendor=v, callback=True, controls=["888800001111"])
    assert r.outcome == verifier.UNAVAILABLE_C2
    assert r.payout_allowed is False
    assert r.escalated is True
    assert r.final_outcome == STEP_UP


def test_unavailable_is_distinguishable_from_failed():
    """
    Two different facts about the world, and an operator has to be able to tell
    them apart: "we asked and they could not" versus "there was nothing to ask".
    """
    failed = chan2(controls=[])
    v = with_accounts(AccountRecord(account_number="888800002222",
                                    settled_payout_count=0, is_primary=True))
    unavailable = chan2(vendor=v, controls=[])
    assert failed.outcome != unavailable.outcome
    assert unavailable.verification_account is None
    assert failed.verification_account == KNOWN_ACCT


def test_the_named_account_is_recorded_for_the_audit():
    """
    "We verified control of an account" and "we verified control of 434392416664,
    which had 14 settled payouts and was added at onboarding" are different
    claims. Only the second is a control.
    """
    r = chan2(controls=[KNOWN_ACCT])
    assert r.verification_account == KNOWN_ACCT
    assert "onboarding" in r.verification_account_basis


# ══ verifier: the API actions a decision maps to ══════════════════════

def test_allow_maps_to_approve():
    acts = verifier.razorpay_actions(ALLOW, "clean", "pout_1", "fa_1")
    assert len(acts) == 1
    assert acts[0]["method"] == "POST"
    assert acts[0]["endpoint"] == "/v1/payouts/pout_1/approve"


def test_block_rejects_and_deactivates_the_fund_account():
    """No rule reaches BLOCK any more; the outcome-to-endpoint mapping stays."""
    acts = verifier.razorpay_actions(BLOCK, "bad", "pout_1", "fa_1")
    assert [a["method"] for a in acts] == ["POST", "PATCH"]
    assert acts[0]["endpoint"] == "/v1/payouts/pout_1/reject"
    assert acts[1]["endpoint"] == "/v1/fund_accounts/fa_1"
    assert acts[1]["body"] == {"active": False}


def test_a_recommended_rejection_never_executes_unattended():
    """
    V2.1. The recommendation has to be visible — a reviewer needs one click, not
    a form to fill in — while nothing may act on it by itself. Both calls carry
    the flag, so an executor that honours it performs neither.
    """
    acts = verifier.razorpay_actions(STEP_UP, "bec", "pout_1", "fa_1",
                                     recommended_action="reject")
    assert [a["method"] for a in acts] == ["POST", "PATCH"]
    assert acts[0]["endpoint"] == "/v1/payouts/pout_1/reject"
    assert all(a["requires_human_confirmation"] for a in acts)


def test_a_plain_hold_still_makes_no_call():
    """Specificity guard: only a recommendation produces a plan to reject."""
    acts = verifier.razorpay_actions(STEP_UP, "checking", "pout_1", "fa_1",
                                     recommended_action=None)
    assert all(a["method"] is None for a in acts)


# ══ V2.1 — verification cannot overturn a rejection recommendation ════

def test_a_passing_callback_cannot_release_a_recommended_rejection():
    """
    The regression that removing automatic rejection creates, and the reason the
    guard exists. R2c, R3 and R4 used to end the case themselves. Now they hold,
    so their cases reach verification for the FIRST time — and without the guard
    a passing channel releases a payout the previous version rejected. Recall
    would fall silently, and the release would read in the audit as an ordinary
    verified change.
    """
    r = verifier.verify(dec(STEP_UP, "R4_bec_pattern",
                            recommended_action="reject"),
                        VENDOR, True, "C1")
    assert r.final_outcome == STEP_UP
    assert r.payout_allowed is False
    assert r.escalated is True


def test_a_planted_account_cannot_clear_a_recommended_rejection():
    """
    BUILD-LOG.md V2.6 in miniature. An attacker who once got an account onto the
    vendor master penny-drops from it, and channel 2 — the authoritative one —
    passes. A previous success used as the credential for the next one.

    Choosing WHICH account the drop must come from is the real fix and is scoped
    as V2.6. This is the floor underneath it: evidence of impersonation is not
    something a rupee clears.
    """
    r = verifier.verify(dec(STEP_UP, "R4_bec_pattern",
                            recommended_action="reject"),
                        VENDOR, True, "C1",
                        requester_controls_accounts=[KNOWN_ACCT], as_of=AS_OF)
    assert r.payout_allowed is False
    assert "R4_bec_pattern" in r.reason


def test_the_guard_does_not_touch_an_ordinary_hold():
    """Specificity: a hold with no recommendation still releases on a callback."""
    r = verifier.verify(dec(STEP_UP, "R5_tier1_inconclusive"), VENDOR, True, "C1")
    assert r.payout_allowed is True
    assert r.final_outcome == ALLOW


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
    # More accounts than vendors now, and every value is a SET of owners.
    assert len(idx) > len(vendors)
    for acct, owners in list(idx.items())[:5]:
        assert isinstance(owners, set) and owners
        for vid in owners:
            assert acct in vendors[vid].all_known_accounts()


def test_the_account_index_keeps_every_owner_of_a_shared_account():
    """
    The v2.0 bug in one assertion. `idx[acct] = vendor_id` in a loop made the
    last writer win, so a payout to a shared account was rejected for whichever
    vendor lost the race — including the corporate groups the engine's own
    comment calls legitimate.
    """
    vendors = pipeline.load_vendors()
    idx = pipeline.build_account_index(vendors)
    shared = [a for a, owners in idx.items() if len(owners) > 1]
    assert shared, ("no account in the master is shared, so this property is "
                    "untested — the generator must emit corporate groups")
    for acct in shared:
        for vid in idx[acct]:
            assert acct in vendors[vid].all_known_accounts()


def test_the_corpus_actually_contains_the_scenarios_it_claims():
    """
    A DIVERSITY floor on the vendor master, not just a shape check.

    Every existing test passed on a master with ONE declared group of three
    vendors sharing ONE account — because a variable named `n` in the domain
    de-duplication loop shadowed the vendor-count parameter, leaving n == 2 for
    `range(max(1, n // 6))`. Twenty groups became one. The corpus generated
    cleanly, all 216 tests passed, every eval ran, and the headline
    "corporate groups 12/12 allowed" was quietly computed over three vendors
    and a single account configuration.

    Shape assertions cannot catch that: one group IS a valid master. Only a
    floor on how much of the scenario the data actually covers can, which is
    the same "coded capability with nothing behind it" check this project keeps
    applying to code, applied to the data instead.
    """
    import collections
    vendors = pipeline.load_vendors()

    domains = collections.Counter(v.known_domain for v in vendors.values())
    assert all(c == 1 for c in domains.values()), (
        "vendor domains are not unique; a domain shared by two unrelated "
        "vendors makes the sender ambiguous and turns triage's vendor "
        "resolution into a coin flip")

    groups = collections.Counter(v.group_id for v in vendors.values() if v.group_id)
    assert len(groups) >= 10, (
        f"only {len(groups)} declared group(s) in the master; the "
        f"corporate-group result would be measured over almost nothing")

    owners = collections.defaultdict(set)
    for v in vendors.values():
        for acct in v.all_known_accounts():
            owners[acct].add(v.vendor_id)
    shared = [a for a, o in owners.items() if len(o) > 1]
    assert len(shared) >= 5, (
        f"only {len(shared)} shared account(s); the reproducible v1 false "
        f"rejection is barely represented")

    # And the shape that would make those cases mislabelled rather than sparse.
    for acct in shared:
        gids = {vendors[v].group_id for v in owners[acct]}
        assert len(gids) == 1 and "" not in gids, (
            f"account {acct} is shared across different groups (or an "
            f"ungrouped vendor); that is the mule pattern, not a group")


def test_every_vendor_has_exactly_one_primary_account():
    for v in pipeline.load_vendors().values():
        assert sum(1 for a in v.accounts if a.is_primary) == 1
        assert v.known_account_number == v.accounts[0].account_number
        assert v.accounts[0].is_primary


def test_a_second_primary_is_a_load_error_not_a_silent_pick():
    """
    known_account_number is DERIVED from the primary row. Two primaries means
    the file does not say which, and choosing one quietly is how a loader ends
    up disagreeing with the data it read.
    """
    import tempfile, csv as _csv
    cols = ["vendor_id", "account_number", "ifsc", "status", "added_on",
            "added_via", "verified_by", "verified_on", "settled_payout_count",
            "last_settled_on", "is_primary"]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "vendor_accounts.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for acct in ("111122223333", "444455556666"):
                w.writerow({c: "" for c in cols} | {
                    "vendor_id": "VEND0001", "account_number": acct,
                    "settled_payout_count": "1", "is_primary": "True"})
        try:
            pipeline.load_vendor_accounts(path)
        except ValueError as e:
            assert "primary" in str(e)
        else:
            raise AssertionError("two primaries loaded without complaint")


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
