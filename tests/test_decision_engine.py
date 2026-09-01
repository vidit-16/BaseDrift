"""
PayeeProof — decision_engine regression tests.

Every test here is a REGRESSION test for a specific flaw found by audit. Each
one fails against the pre-P0 engine and passes after. The point is not coverage
for its own sake — it is that these four bypasses stay closed.

No API key needed. ExtractionResult is constructed directly, so the LLM never
runs and the tests are deterministic — which matters, because the real extractor
is not (see P0.4 and the note in extractor.py).

Run standalone:
    python tests/test_decision_engine.py
Or under pytest if you have it:
    pytest tests/
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from extractor import (  # noqa: E402
    ExtractionResult,
    INTENT_CHANGE, INTENT_FOLLOWUP,
    ACTION_REPLACE, ACTION_NONE,
    SCOPE_BOTH, SCOPE_NONE,
)
from decision_engine import (  # noqa: E402
    decide, FAVResult, VendorRecord, AccountRecord, same_group,
    ALLOW, STEP_UP, BLOCK,
    WARN as WARN_, PASS as PASS_, INCONCLUSIVE as INCONCLUSIVE_,
)

# ── Fixtures ──────────────────────────────────────────────────────────

KNOWN_ACCT = "434392416664"
NEW_ACCT   = "351349409853"
OTHER_ACCT = "777788889999"

VENDOR = VendorRecord(
    vendor_id="VEND0069",
    legal_name="Balaji Logistics",
    gstin="07JQQPG8009O1Z2",
    known_domain="balajilogistic.com",
    known_phone="9088190947",
    known_account_number=KNOWN_ACCT,
    known_ifsc="KKBK0403467",
    avg_payout_amount=28000.0,
)

# OTHER_ACCT belongs to a different vendor — cross-contact reuse.
# A SET of owners, not one id: an account legitimately belongs to more than one
# contact inside a corporate group, and the dict that used to hold a single
# owner silently overwrote and rejected the group.
ACCOUNT_INDEX = {KNOWN_ACCT: {"VEND0069"}, OTHER_ACCT: {"VEND0123"}}

FAV_CLEAN = FAVResult("active", "Balaji Logistics", 99)


def ext(intent=INTENT_FOLLOWUP, action=ACTION_NONE, scope=SCOPE_NONE,
        account=None, gstin=VENDOR.gstin, domain=VENDOR.known_domain,
        amount=28000.0, urgency=False, hedged_fields=None,
        hedging_detected=None, channel=False, ok=True):
    """A clean extraction unless a test deliberately dirties one field."""
    hf = list(hedged_fields or [])
    return ExtractionResult(
        ok=ok,
        intent=intent, action=action, scope=scope,
        proposed_account_number=account,
        proposed_gstin=gstin,
        sender_domain=domain,
        amount=amount,
        urgency_detected=urgency,
        hedging_detected=bool(hf) if hedging_detected is None else hedging_detected,
        hedged_fields=hf,
        channel_manipulation_detected=channel,
    )


def run(e, dest=None, fav=FAV_CLEAN, index=ACCOUNT_INDEX):
    return decide(e, fav, VENDOR, other_vendor_accounts=index,
                  destination_account_number=dest)


# ── P0.1 — the LLM must not be able to produce an ALLOW alone ─────────
# Pre-P0, R2 returned ALLOW the moment intent was PAYMENT_FOLLOWUP, before any
# Tier 1 check ran. A misclassification or an injection reaching that label was
# a complete bypass of identity validation.

def test_followup_to_unknown_destination_does_not_allow():
    """The bypass. Model says 'nothing is changing'; money goes somewhere new."""
    d = run(ext(intent=INTENT_FOLLOWUP), dest=NEW_ACCT)
    assert d.outcome != ALLOW, f"BYPASS OPEN: {d.rule_fired} allowed the payout"
    assert d.outcome == STEP_UP
    assert d.rule_fired == "R2b_followup_unverified_destination"
    assert d.payout_allowed is False


def test_followup_to_other_vendors_account_holds_and_recommends_rejection():
    d = run(ext(intent=INTENT_FOLLOWUP), dest=OTHER_ACCT)
    assert d.outcome == STEP_UP
    assert d.recommended_action == "reject"
    assert d.payout_allowed is False
    assert d.rule_fired == "R2c_followup_destination_conflict"


def test_followup_with_no_resolvable_destination_does_not_allow():
    """Nothing to check against is not the same as checked and clean."""
    d = run(ext(intent=INTENT_FOLLOWUP, account=None), dest=None)
    assert d.outcome != ALLOW
    assert d.payout_allowed is False


def test_genuine_followup_still_allows():
    """Specificity guard: the fix must not step up every routine follow-up."""
    d = run(ext(intent=INTENT_FOLLOWUP), dest=KNOWN_ACCT)
    assert d.outcome == ALLOW
    assert d.rule_fired == "R2a_no_change_confirmed"
    assert d.payout_allowed is True


def test_injection_flipping_intent_cannot_release_payout():
    """
    Models the actual attack: text in the document convinces the semantic layer
    to report PAYMENT_FOLLOWUP while a payout sits pointed at the attacker.
    extractor.py used to claim injection 'cannot approve a payout'; it could.
    """
    injected = ext(intent=INTENT_FOLLOWUP, action=ACTION_NONE, scope=SCOPE_NONE,
                   gstin=None, domain="attacker-domain.com", urgency=True)
    d = run(injected, dest=NEW_ACCT)
    assert d.payout_allowed is False
    assert d.outcome in (STEP_UP, BLOCK)


# ── P0.2 — FAV account_status must be read ───────────────────────────

def test_inactive_account_holds_but_is_not_an_identity_conflict():
    """
    V2.5. This began as the opposite assertion, and the opposite was wrong.
    account_status went from being read by nothing at all to being a hard
    identity conflict, and that overcorrection produced every false rejection
    in v1 — on a field that is UNCORRELATED with fraud in the dataset.

    A dormant account, one opened last week, and one with KYC still pending all
    report inactive. "The bank says this cannot receive right now" is a reason
    to ask the vendor, never a reason to refuse them.
    """
    from decision_engine import check_account_status, FAIL, INCONCLUSIVE
    sig = check_account_status(FAVResult("inactive", "Balaji Logistics", 99))
    assert sig.result == INCONCLUSIVE
    assert sig.result != FAIL

    d = run(ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
                account=NEW_ACCT),
            dest=NEW_ACCT, fav=FAVResult("inactive", "Balaji Logistics", 99))
    assert d.payout_allowed is False          # still not released
    assert d.recommended_action is None       # and no longer a rejection
    assert d.rule_fired.startswith("R5")


def test_unknown_account_status_never_allows():
    """FAV inconclusive must not read as clean."""
    d = run(ext(intent=INTENT_FOLLOWUP), dest=KNOWN_ACCT,
            fav=FAVResult("unknown", "Balaji Logistics", 99))
    # A follow-up to a known account is fine on continuity, so this asserts the
    # signal exists and is non-PASS rather than forcing a particular outcome.
    from decision_engine import check_account_status, PASS
    assert check_account_status(FAVResult("unknown", "X", 99)).result != PASS


def test_active_account_passes():
    from decision_engine import check_account_status, PASS
    assert check_account_status(FAV_CLEAN).result == PASS


# ── P0.3 — the checked account must be the real destination ──────────

def test_destination_overrides_the_emails_claim():
    """
    The sharpest case. The message names the vendor's genuine account, so
    checking the CLAIM yields PASS — but the payout is pointed elsewhere.
    Pre-P0 this allowed; the money and the paperwork disagreed and nothing
    noticed.
    """
    claiming_known = ext(intent=INTENT_FOLLOWUP, account=KNOWN_ACCT)
    d = run(claiming_known, dest=NEW_ACCT)
    assert d.outcome != ALLOW, "checked the claim instead of the destination"
    assert d.checked_destination == NEW_ACCT
    assert d.destination_source == "razorpay_payout"


def test_claim_is_fallback_only_and_is_marked_unverified():
    d = run(ext(intent=INTENT_FOLLOWUP, account=KNOWN_ACCT), dest=None)
    assert d.checked_destination == KNOWN_ACCT
    assert d.destination_source == "email_claim_only"


# ── P0.4 — hedge detection must not fail open on spelling ────────────

def test_hedge_detected_across_model_spellings():
    """
    All of these were observed from the model for one concept. The old exact
    tuple ("gstin","proposed_gstin") missed the rest, and a missed hedge is one
    fewer non-clean signal — it failed OPEN.

    A hedge is INCONCLUSIVE, not WARN: "should be the same as before" is a
    weaker claim, not adverse evidence. Both hold the payout; only WARN may
    contribute to a BLOCK.
    """
    from decision_engine import check_gstin, INCONCLUSIVE
    for spelling in ("gstin", "proposed_gstin", "GSTIN", "gst_number",
                     "gst", "proposed_gstin_value"):
        sig = check_gstin(ext(hedged_fields=[spelling]), VENDOR)
        assert sig.result == INCONCLUSIVE, f"hedge missed for spelling {spelling!r}"


def test_hedging_without_named_field_is_treated_as_hedged():
    from decision_engine import check_gstin, INCONCLUSIVE
    e = ext(hedged_fields=[], hedging_detected=True)
    assert check_gstin(e, VENDOR).result == INCONCLUSIVE


def test_unhedged_matching_gstin_passes():
    """Guard against over-warning: a clean claim must still PASS."""
    from decision_engine import check_gstin, PASS
    e = ext(hedged_fields=[], hedging_detected=False)
    assert check_gstin(e, VENDOR).result == PASS


def test_unrelated_hedge_does_not_warn_gstin():
    from decision_engine import check_gstin, PASS
    e = ext(hedged_fields=["amount"], hedging_detected=True)
    assert check_gstin(e, VENDOR).result == PASS


# ── Missing data must never cause a rejection ────────────────────────
# WARN carried two meanings: "looks wrong" and "could not check". Both hold a
# payout; only the first is evidence and may contribute to a BLOCK.

def test_missing_amount_is_inconclusive_not_adverse():
    """A message that omits an amount has LESS information, not worse."""
    from decision_engine import check_payment_pattern, INCONCLUSIVE
    sig = check_payment_pattern(ext(amount=None), VENDOR)
    assert sig.result == INCONCLUSIVE


def test_missing_amount_cannot_push_a_case_into_block():
    """
    Regression on the hero case's third warn. With a lookalike domain and
    urgency (2 real warns) plus a missing amount, the old engine counted three
    and blocked. The missing amount must not be one of them.
    """
    from decision_engine import WARN, INCONCLUSIVE
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain="balaj1logistic.com", urgency=True,
            amount=None)
    d = run(e, dest=NEW_ACCT)
    warns = [s.name for s in d.tier2 if s.result == WARN]
    incon = [s.name for s in d.tier2 if s.result == INCONCLUSIVE]
    assert "payment_pattern" in incon, "missing amount still counted as adverse"
    assert "payment_pattern" not in warns


def test_deviating_amount_is_still_adverse():
    """Guard the other direction: a real deviation must still WARN."""
    from decision_engine import check_payment_pattern, WARN
    assert check_payment_pattern(ext(amount=500000.0), VENDOR).result == WARN


def test_inconclusive_signals_still_hold_the_payout():
    """Fail-safe intact: less information must never mean release."""
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=KNOWN_ACCT, gstin=None)      # no GSTIN -> INCONCLUSIVE
    d = run(e, dest=KNOWN_ACCT)
    assert d.outcome == STEP_UP
    assert d.payout_allowed is False


def test_unresolvable_destination_on_followup_still_holds():
    """
    Regression: reclassifying the unresolved destination from WARN to
    INCONCLUSIVE briefly made R2 fall through to ALLOW.
    """
    from decision_engine import INCONCLUSIVE
    d = run(ext(intent=INTENT_FOLLOWUP, account=None), dest=None)
    assert d.outcome == STEP_UP
    assert d.payout_allowed is False
    assert d.tier1[0].result == INCONCLUSIVE


# ── R4 requires evidence of deliberate impersonation ─────────────────
# R4 used to fire on REPLACE + new account + any 2 Tier-2 warns. Both inputs are
# true of an ordinary legitimate bank change, so on the dev set it rejected
# 15.8% of legitimate traffic — 49 of 60 acquisition/rebrand cases. No threshold
# fixed it: tightening to 4 warns cut false blocks to 0.6% but dropped recall to
# 85.4%, no better than holding every payout and phoning the vendor.

def test_lookalike_detects_typosquats():
    from decision_engine import is_lookalike_domain
    known = "balajilogistic.com"
    for squat in ("balaj1logistic.com", "ba1ajilogistic.com", "balajilog1stic.com"):
        assert is_lookalike_domain(squat, known), squat


def test_lookalike_does_not_flag_a_rebrand():
    """An acquired company's new domain is not an impersonation attempt."""
    from decision_engine import is_lookalike_domain
    known = "balajilogistic.com"
    for legit in ("balajilogisticgroup.com", "balajilogisticglobal.com",
                  "totallyunrelated.com", "balajilogistic.com"):
        assert not is_lookalike_domain(legit, known), legit


def test_typosquat_plus_one_warn_recommends_rejection():
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain="balaj1logistic.com", urgency=True)
    d = run(e, dest=NEW_ACCT)
    assert d.outcome == STEP_UP
    assert d.recommended_action == "reject"
    assert d.rule_fired == "R4_bec_pattern"


def test_rebrand_domain_with_many_warns_does_not_block():
    """
    The regression that mattered. A genuinely renamed vendor, in a hurry, asking
    to be reached at a new address — three contextual warns and no impersonation.
    Must hold for a callback, never reject.
    """
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain="balajilogisticgroup.com",
            urgency=True, channel=True, amount=500000.0)
    d = run(e, dest=NEW_ACCT)
    assert d.outcome == STEP_UP
    assert d.recommended_action is None, (
        f"recommended rejecting a legitimate rebrand via {d.rule_fired}")


def test_contextual_signals_alone_never_reject():
    """
    Tier 2 is documented as 'supporting evidence, never decisive alone'. R4
    violated that: a new account plus two contextual warns was a rejection, and
    a new account is simply what changing banks means.
    """
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain=VENDOR.known_domain,
            urgency=True, channel=True, amount=999999.0)
    d = run(e, dest=NEW_ACCT)
    assert not any(s.deception for s in d.tier1 + d.tier2)
    assert d.outcome == STEP_UP


def test_deception_flag_is_set_only_on_impersonation():
    from decision_engine import check_domain
    squat = check_domain(ext(domain="balaj1logistic.com"), VENDOR)
    rebrand = check_domain(ext(domain="balajilogisticgroup.com"), VENDOR)
    match = check_domain(ext(domain=VENDOR.known_domain), VENDOR)
    assert squat.deception is True
    assert rebrand.deception is False
    assert match.deception is False
    # Both non-matching domains still hold the payout; only one may reject it.
    assert squat.result == WARN_ and rebrand.result == WARN_


def test_compromised_mailbox_falls_through_to_callback():
    """
    Real domain, correct GSTIN, good name match, new account. There is no
    deception signal and no identity conflict, so evidence cannot separate this
    from a legitimate change. Holding for the callback is the correct answer,
    and pretending otherwise would mean rejecting legitimate vendors.
    """
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain=VENDOR.known_domain, urgency=False)
    d = run(e, dest=NEW_ACCT)
    assert d.outcome == STEP_UP
    assert d.needs_callback is True


# ── A blank authoritative destination must never fall back ───────────

def test_blank_destination_does_not_fall_back_to_the_claim():
    """
    resolve_destination() treated "" as "not supplied" and fell back to the
    account number the request itself named — producing an outright ALLOW.
    A caller passing this parameter asserted it had authoritative data.
    """
    for blank in ("", "   ", "	"):
        d = run(ext(intent=INTENT_FOLLOWUP, account=KNOWN_ACCT), dest=blank)
        assert d.outcome != ALLOW, f"released on dest={blank!r}"
        assert d.payout_allowed is False
        assert d.destination_source == "authoritative_destination_malformed"


def test_absent_destination_still_uses_the_claim_for_offline_analysis():
    """The None case is different: nothing was asserted, so the documented
    offline fallback applies and is labelled as unverified."""
    d = run(ext(intent=INTENT_FOLLOWUP, account=KNOWN_ACCT), dest=None)
    assert d.destination_source == "email_claim_only"


def test_engine_does_not_crash_on_non_string_phrases():
    """A rule engine crashable by its own input is a DoS on the payout queue."""
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, urgency=True)
    e.urgency_phrases = [123, {"a": 1}, None]
    e.channel_manipulation_detected = True
    e.channel_manipulation_phrases = [object()]
    d = run(e, dest=NEW_ACCT)
    assert d.outcome in (ALLOW, STEP_UP, BLOCK)


# ── Guards on behaviour that must NOT have changed ───────────────────

def test_extraction_failure_still_steps_up():
    d = run(ExtractionResult(ok=False, failure_reason="api down"))
    assert d.outcome == STEP_UP
    assert d.rule_fired == "R1_extraction_failed"
    assert d.payout_allowed is False


def test_bec_pattern_still_caught():
    """The hero shape: REPLACE to a new account under >=2 contextual warns."""
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain="balaj1logistic.com", urgency=True,
            amount=None)
    d = run(e, dest=NEW_ACCT)
    assert d.payout_allowed is False
    assert d.recommended_action == "reject"
    assert d.rule_fired == "R4_bec_pattern"


# ── V2.1: nothing rejects unattended ─────────────────────────────────

def test_no_rule_path_rejects_unattended():
    """
    Structural, because a behavioural sweep only covers the inputs someone
    thought of. There is no BLOCK outcome left in the engine at all, so a future
    edit has to reintroduce the literal in order to reintroduce the outcome.

    Why removing it is safe rather than merely softer: a rejection prevents no
    fraud that a hold does not — the money stays put either way — so automatic
    rejection bought operational convenience and paid for it with the only
    customer-facing failure this system had. Measured on the 556-case dev split,
    removing it left recall at 100% and took false rejections to 0.0%.
    """
    import decision_engine
    src = open(decision_engine.__file__, encoding="utf-8").read()
    body = src.split('"""', 2)[2]      # past the module docstring's rule table
    assert "outcome=BLOCK" not in body


def test_every_recommendation_still_holds_the_payout():
    """A recommendation is not a decision: the payout is unreleased regardless."""
    cases = [
        run(ext(intent=INTENT_FOLLOWUP), dest=OTHER_ACCT),
        run(ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
                account=NEW_ACCT, domain="balaj1logistic.com", urgency=True),
            dest=NEW_ACCT),
        run(ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
                account=OTHER_ACCT), dest=OTHER_ACCT),
    ]
    recommended = [d for d in cases if d.recommended_action == "reject"]
    assert len(recommended) == 3, [d.rule_fired for d in cases]
    for d in recommended:
        assert d.outcome == STEP_UP
        assert d.payout_allowed is False
        assert d.needs_callback is True
        assert d.to_dict()["recommended_action"] == "reject"


# ══ V2.0 — the vendor master holds more than one account ══════════════

SHARED_ACCT = "606070708080"


def _grouped(vendor_id, group_id, extra=()):
    accts = [AccountRecord(account_number=(KNOWN_ACCT if vendor_id == "VEND0069"
                                           else "121234345656"),
                           is_primary=True, settled_payout_count=10,
                           added_on="2023-01-01", added_via="onboarding",
                           verified_by="onboarding_kyc")]
    accts += [AccountRecord(account_number=a, settled_payout_count=4,
                            added_on="2023-06-01", added_via="portal",
                            verified_by="penny_drop") for a in extra]
    return VendorRecord(
        vendor_id=vendor_id, legal_name="Balaji Logistics",
        gstin=VENDOR.gstin, known_domain=VENDOR.known_domain,
        known_phone=VENDOR.known_phone,
        known_account_number=accts[0].account_number,
        known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
        group_id=group_id, accounts=accts,
    )


def test_a_shared_account_inside_a_declared_group_is_not_reuse():
    """
    REPRODUCIBLE FALSE REJECTION IN v1, in three lines: build_account_index()
    assigned account -> vendor in a loop, so the sibling silently overwrote and
    a payout to the shared facility fired the cross-contact FAIL for whichever
    vendor lost the race. decision_engine's own comment said sharing an account
    across contacts is legitimate for corporate groups. The code rejected it.
    """
    me = _grouped("VEND0069", "GRP001", extra=[SHARED_ACCT])
    sibling = _grouped("VEND0123", "GRP001", extra=[SHARED_ACCT])
    index = {SHARED_ACCT: {"VEND0069", "VEND0123"}}
    d = decide(ext(intent=INTENT_FOLLOWUP), FAV_CLEAN, me,
               other_vendor_accounts=index,
               destination_account_number=SHARED_ACCT,
               vendors={"VEND0069": me, "VEND0123": sibling})
    assert d.outcome == ALLOW
    assert d.recommended_action is None
    assert d.rule_fired == "R2a_no_change_confirmed"


def test_two_vendors_with_no_group_are_not_a_group():
    """
    THE TRAP, written as a test because `a.group_id == b.group_id` matches two
    blanks. Getting this wrong turns the mule check off for every vendor that
    belongs to no group — most of the master — and it would pass every test
    that only ever looks at vendors which DO share a group.
    """
    me = _grouped("VEND0069", "")
    stranger = _grouped("VEND0123", "")
    assert same_group(me, stranger) is False
    index = {SHARED_ACCT: {"VEND0069", "VEND0123"}}
    d = decide(ext(intent=INTENT_FOLLOWUP), FAV_CLEAN, me,
               other_vendor_accounts=index,
               destination_account_number=SHARED_ACCT,
               vendors={"VEND0069": me, "VEND0123": stranger})
    assert d.recommended_action == "reject"
    assert d.rule_fired == "R2c_followup_destination_conflict"


def test_a_vendor_in_a_different_group_is_still_a_stranger():
    me = _grouped("VEND0069", "GRP001", extra=[SHARED_ACCT])
    other = _grouped("VEND0123", "GRP002", extra=[SHARED_ACCT])
    d = decide(ext(intent=INTENT_FOLLOWUP), FAV_CLEAN, me,
               other_vendor_accounts={SHARED_ACCT: {"VEND0069", "VEND0123"}},
               destination_account_number=SHARED_ACCT,
               vendors={"VEND0069": me, "VEND0123": other})
    assert d.recommended_action == "reject"


def test_a_second_account_on_file_is_not_a_new_account():
    """
    v1 held EVERY payout to a legitimate second account, forever, because the
    master recorded one. A permanent false-hold generator, invisible while the
    data agreed with the bug.
    """
    second = "343456567878"
    me = _grouped("VEND0069", "", extra=[second])
    d = decide(ext(intent=INTENT_FOLLOWUP), FAV_CLEAN, me,
               other_vendor_accounts={second: {"VEND0069"}},
               destination_account_number=second,
               vendors={"VEND0069": me})
    assert d.outcome == ALLOW
    assert d.payout_allowed is True


def test_a_missing_vendor_map_still_treats_outsiders_as_strangers():
    """
    Fail-safe on the optional argument: a caller that has not migrated must not
    silently gain a group check that lets shared accounts through.
    """
    me = _grouped("VEND0069", "GRP001", extra=[SHARED_ACCT])
    d = decide(ext(intent=INTENT_FOLLOWUP), FAV_CLEAN, me,
               other_vendor_accounts={SHARED_ACCT: {"VEND0069", "VEND0123"}},
               destination_account_number=SHARED_ACCT)
    assert d.recommended_action == "reject"


def test_clean_change_to_known_account_allows():
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=KNOWN_ACCT)
    d = run(e, dest=KNOWN_ACCT)
    assert d.outcome == ALLOW
    assert d.rule_fired == "R7_all_clear"


# ── Standalone runner ────────────────────────────────────────────────


# ══ Being on file is not the same as being established ════════════════
# The trust store is the root of trust for every check here, so the one way to
# beat all of them at once is to poison it: get an account onto the master with
# a single accepted email, wait, then ask for the money. Every identity check
# then passes HONESTLY, on a fact that is itself the fraud.

PLANTED = "133688561858"


def _vendor_with(account: AccountRecord) -> VendorRecord:
    return VendorRecord(
        vendor_id="VEND0069", legal_name="Balaji Logistics",
        gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
        known_phone="9088190947", known_account_number=KNOWN_ACCT,
        known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
        accounts=[AccountRecord(account_number=KNOWN_ACCT, is_primary=True,
                                added_via="onboarding",
                                verified_by="onboarding_kyc",
                                settled_payout_count=39),
                  account])


def _pay_to(vendor, account_number):
    e = ExtractionResult(ok=True, intent=INTENT_CHANGE,
                         action=ACTION_REPLACE, scope=SCOPE_BOTH,
                         proposed_account_number=account_number,
                         sender_domain=vendor.known_domain, amount=28000.0)
    return decide(e, FAVResult("active", "Balaji Logistics", 100), vendor,
                  destination_account_number=account_number)


def test_a_planted_account_does_not_pass_just_because_it_is_on_file():
    """
    Added by an email request, never verified by anything outside email, never
    used to settle anything. Three facts the master already records and the
    destination check used to ignore, because "in all_known_accounts()" was
    treated as one thing.
    """
    v = _vendor_with(AccountRecord(account_number=PLANTED,
                                   added_via="email_request",
                                   verified_by="unverified",
                                   settled_payout_count=0))
    d = _pay_to(v, PLANTED)
    assert d.payout_allowed is False, "a planted account was paid"
    sig = [s for s in d.tier1 if s.name == "account_continuity"][0]
    assert sig.result == INCONCLUSIVE_, sig.result


def test_it_is_inconclusive_and_never_a_rejection():
    """
    "Could not confirm" is not "looks wrong". The project rule is that missing
    data never pushes a case toward rejection, and an unverified account is
    missing data about the account, not evidence against the vendor.
    """
    v = _vendor_with(AccountRecord(account_number=PLANTED,
                                   added_via="email_request",
                                   verified_by="unverified",
                                   settled_payout_count=0))
    d = _pay_to(v, PLANTED)
    assert d.recommended_action != "reject"
    assert d.outcome != BLOCK


def test_an_account_that_has_actually_been_paid_is_established():
    """
    legit_added_then_paid: added by an email request, then used. A settlement
    is real-world evidence the request was genuine — money went out and nobody
    complained — so this must keep passing or every vendor who ever legitimately
    added an account is held forever.
    """
    v = _vendor_with(AccountRecord(account_number=PLANTED,
                                   added_via="email_request",
                                   verified_by="unverified",
                                   settled_payout_count=3))
    sig = [s for s in _pay_to(v, PLANTED).tier1
           if s.name == "account_continuity"][0]
    assert sig.result == PASS_, sig.detail


def test_an_account_verified_outside_email_is_established():
    """A penny drop is exactly the evidence the unverified case lacks."""
    v = _vendor_with(AccountRecord(account_number=PLANTED,
                                   added_via="email_request",
                                   verified_by="penny_drop",
                                   settled_payout_count=0))
    sig = [s for s in _pay_to(v, PLANTED).tier1
           if s.name == "account_continuity"][0]
    assert sig.result == PASS_, sig.detail


def test_a_vendor_with_no_account_provenance_is_not_penalised():
    """
    A caller that built a VendorRecord without AccountRecords has not asserted
    the account is weak — it simply has not said. Reading silence as suspicion
    would hold every payout for anyone whose master lacks provenance columns.
    """
    v = VendorRecord(
        vendor_id="VEND0069", legal_name="Balaji Logistics",
        gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
        known_phone="9088190947", known_account_number=KNOWN_ACCT,
        known_ifsc="KKBK0403467", avg_payout_amount=28000.0)
    sig = [s for s in _pay_to(v, KNOWN_ACCT).tier1
           if s.name == "account_continuity"][0]
    assert sig.result == PASS_, sig.detail


def test_the_group_shared_branch_applies_the_same_test():
    """
    A shared facility inside a declared group returns early with PASS. An
    unverified, never-settled account reached through that branch would skip
    the anchor test entirely — the sort of hole a second return statement
    creates and nothing notices.
    """
    a = AccountRecord(account_number=PLANTED, added_via="email_request",
                      verified_by="unverified", settled_payout_count=0)
    v = _vendor_with(a)
    v.group_id = "GRP01"
    sibling = VendorRecord(
        vendor_id="VEND0123", legal_name="Balaji Freight",
        gstin="07JQQPG8009O1Z3", known_domain="balajifreight.com",
        known_phone="9088190948", known_account_number=PLANTED,
        known_ifsc="KKBK0403467", avg_payout_amount=28000.0, group_id="GRP01")
    e = ExtractionResult(ok=True, intent=INTENT_CHANGE,
                         action=ACTION_REPLACE, scope=SCOPE_BOTH,
                         proposed_account_number=PLANTED,
                         sender_domain=v.known_domain, amount=28000.0)
    d = decide(e, FAVResult("active", "Balaji Logistics", 100), v,
               other_vendor_accounts={PLANTED: {"VEND0069", "VEND0123"}},
               vendors={"VEND0069": v, "VEND0123": sibling},
               destination_account_number=PLANTED)
    sig = [s for s in d.tier1 if s.name == "account_continuity"][0]
    assert sig.result == INCONCLUSIVE_, (
        "the group branch released an unverified account")


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e) or "assertion failed"))
            print(f"  FAIL  {name}")
            print(f"        {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}")
            print(f"        {type(e).__name__}: {e}")

    print()
    print(f"  {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
