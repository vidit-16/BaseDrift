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
    decide, FAVResult, VendorRecord,
    ALLOW, STEP_UP, BLOCK,
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
ACCOUNT_INDEX = {KNOWN_ACCT: "VEND0069", OTHER_ACCT: "VEND0123"}

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


def test_followup_to_other_vendors_account_blocks():
    d = run(ext(intent=INTENT_FOLLOWUP), dest=OTHER_ACCT)
    assert d.outcome == BLOCK
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

def test_inactive_account_blocks_despite_perfect_name_match():
    d = run(ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
                account=NEW_ACCT),
            dest=NEW_ACCT, fav=FAVResult("inactive", "Balaji Logistics", 99))
    assert d.outcome == BLOCK
    assert "account_status" in d.triggered_by


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


# ── Guards on behaviour that must NOT have changed ───────────────────

def test_extraction_failure_still_steps_up():
    d = run(ExtractionResult(ok=False, failure_reason="api down"))
    assert d.outcome == STEP_UP
    assert d.rule_fired == "R1_extraction_failed"
    assert d.payout_allowed is False


def test_bec_pattern_still_blocks():
    """The hero shape: REPLACE to a new account under >=2 contextual warns."""
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=NEW_ACCT, domain="balaj1logistic.com", urgency=True,
            amount=None)
    d = run(e, dest=NEW_ACCT)
    assert d.outcome == BLOCK
    assert d.rule_fired == "R4_bec_pattern"


def test_clean_change_to_known_account_allows():
    e = ext(intent=INTENT_CHANGE, action=ACTION_REPLACE, scope=SCOPE_BOTH,
            account=KNOWN_ACCT)
    d = run(e, dest=KNOWN_ACCT)
    assert d.outcome == ALLOW
    assert d.rule_fired == "R7_all_clear"


# ── Standalone runner ────────────────────────────────────────────────

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
