"""
PayeeProof — the case file, and the two-person rule.

The buttons on the dashboard are the moment this system stops being a classifier
and starts being a control. What matters is not that they exist but what they
refuse to do:

  1  NOTHING RELEASES WITHOUT A RECORDED VERIFICATION. Fail-safe by inaction —
     a held payment stays held until somebody establishes something, not until
     somebody grows tired of it.

  2  THE VERIFIER CANNOT BE THE RELEASER. If one person can type "supplier
     confirmed by phone" and then release, every upstream control is theatre,
     because the attack this whole system exists to stop is somebody
     substituting a destination account.

  3  A NEGATIVE OUTCOME IS STICKY. A supplier who denies the request and is
     later marked confirmed is a worse case than one who simply denied it.

  4  REJECTION IS NOT SEGREGATED, DELIBERATELY. The two-person rule protects
     money leaving. Refusing to pay releases nothing, so applying it there
     would slow the safe direction and buy nothing.

No API key, no network, no web server.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import casefile as C  # noqa: E402


def _case(*pairs):
    """A case file built from (action, actor) pairs, in order."""
    actions = []
    for action, actor in pairs:
        C.record(actions, action, actor)
    return actions


# ══ The state is a fold, never a stored field ═════════════════════════

def test_an_untouched_case_is_open():
    assert C.state_of([]) == "open"


def test_an_automatically_released_payout_is_not_work():
    """
    A payout the engine allowed has no case to work. Showing it as "held,
    nothing recorded" would put clean traffic in the operator's queue, which is
    how a queue stops being read at all.
    """
    assert C.state_of([], final_outcome="ALLOW") == "no_action"


def test_recording_a_callback_outcome_verifies_the_case():
    assert C.state_of(_case(("callback_confirmed", "priya"))) == "verified"


def test_asking_for_the_rupee_is_not_the_same_as_receiving_it():
    """
    The demand and its outcome are separate facts. Collapsing them would let a
    case look verified because somebody asked.
    """
    assert C.state_of(_case(("proof_requested", "priya"))) == "awaiting_proof"
    assert C.state_of(_case(("proof_requested", "priya"),
                            ("proof_received", "priya"))) == "verified"


def test_a_denial_outweighs_an_earlier_confirmation():
    """
    Sequence, not last-write-wins. A supplier who confirms and then denies is
    evidence of a problem, and a fold that takes the most recent fact would
    quietly turn that into a release.
    """
    actions = _case(("callback_confirmed", "priya"),
                    ("callback_denied", "priya"))
    assert C.state_of(actions) == "contested"


def test_a_confirmation_after_a_denial_does_not_wash_it_out():
    actions = _case(("callback_denied", "priya"),
                    ("callback_confirmed", "priya"))
    assert C.state_of(actions) == "contested"


def test_a_resolution_ends_the_case():
    actions = _case(("callback_confirmed", "priya"), ("released", "rahul"))
    assert C.state_of(actions) == "released"


# ══ What the buttons refuse to do ═════════════════════════════════════

def test_nothing_releases_without_a_recorded_verification():
    ok, why = C.may_release([], "rahul")
    assert not ok
    assert "verified" in why.lower()


def test_an_unreachable_supplier_does_not_release():
    """
    "Could not reach them" is the absence of evidence, and the system's whole
    posture is that absence of evidence holds. It must not drift into a
    release just because time passed.
    """
    ok, _ = C.may_release(_case(("callback_unreachable", "priya")), "rahul")
    assert not ok


def test_the_person_who_verified_cannot_release():
    """The two-person rule, and the single most important line in this file."""
    actions = _case(("callback_confirmed", "priya"))
    ok, why = C.may_release(actions, "priya")
    assert not ok, "the verifier released their own case"
    assert "cannot also release" in why


def test_a_second_person_can_release_the_same_case():
    actions = _case(("callback_confirmed", "priya"))
    ok, why = C.may_release(actions, "rahul")
    assert ok, why


def test_the_rule_survives_a_verifier_hiding_behind_a_second_action():
    """
    Recording something else afterwards must not launder the first record.
    `verifiers()` collects everyone who ever recorded an outcome, not whoever
    recorded the latest one.
    """
    actions = _case(("callback_confirmed", "priya"),
                    ("proof_requested", "rahul"),
                    ("proof_received", "rahul"))
    ok, _ = C.may_release(actions, "priya")
    assert not ok
    ok, _ = C.may_release(actions, "rahul")
    assert not ok
    ok, why = C.may_release(actions, "meera")
    assert ok, why


def test_whitespace_and_case_do_not_defeat_the_rule():
    actions = _case(("callback_confirmed", "priya"))
    for spelling in ("  priya  ", "Priya", "PRIYA"):
        ok, _ = C.may_release(actions, spelling)
        assert not ok, f"{spelling!r} defeated segregation of duties"


def test_a_contested_case_cannot_be_released_by_anyone():
    actions = _case(("callback_denied", "priya"))
    for actor in ("priya", "rahul", "meera"):
        ok, _ = C.may_release(actions, actor)
        assert not ok, f"{actor} released a contested case"


def test_a_contested_case_says_why_it_is_contested():
    """
    Both the contested check and the not-verified check refuse this, so the
    refusal survives losing either one — but they give different reasons, and
    "verification did not hold up" is a different instruction to an operator
    than "nothing is verified yet". The second sounds like a step was skipped;
    the first says the supplier failed it. Asserting the message is what makes
    the contested branch load-bearing rather than decorative.
    """
    ok, why = C.may_release(_case(("callback_denied", "priya")), "rahul")
    assert not ok
    assert "did not hold up" in why, why


def test_a_resolved_case_cannot_be_released_again():
    actions = _case(("callback_confirmed", "priya"), ("released", "rahul"))
    ok, _ = C.may_release(actions, "meera")
    assert not ok


# ══ Rejection, deliberately unsegregated ══════════════════════════════

def test_anyone_can_reject_at_any_point():
    ok, _ = C.may_reject([])
    assert ok, "an untouched case could not be refused"
    ok, _ = C.may_reject(_case(("callback_confirmed", "priya")))
    assert ok


def test_a_resolved_case_cannot_be_rejected_again():
    ok, _ = C.may_reject(_case(("rejected", "priya")))
    assert not ok


# ══ The log itself ════════════════════════════════════════════════════

def test_an_action_outside_the_vocabulary_is_refused():
    """
    A free-text action field would let the audit trail say anything, which is
    the same as saying nothing.
    """
    try:
        C.record([], "approve_everything", "priya")
    except ValueError:
        return
    raise AssertionError("an unknown action was recorded")


def test_every_recorded_action_names_a_person():
    """An anonymous record cannot support a two-person rule."""
    for actor in ("", "   ", None):
        try:
            C.record([], "callback_confirmed", actor)
        except ValueError:
            continue
        raise AssertionError(f"recorded an action with actor {actor!r}")


def test_the_summary_tells_the_operator_what_to_do_next():
    """Status without a next step leaves the queue unactionable."""
    for actions, expect in (([], "open"),
                            (_case(("proof_requested", "priya")),
                             "awaiting_proof"),
                            (_case(("callback_confirmed", "priya")),
                             "verified")):
        s = C.summary(actions)
        assert s["state"] == expect
        assert s["next_step"], f"no next step for {expect}"


def test_every_state_has_a_label():
    """A state with no label reaches the screen as a raw identifier."""
    for state in ("open", "contacting", "awaiting_proof", "verified",
                  "contested", "released", "rejected", "no_action"):
        assert C.STATE_LABEL.get(state), state


def test_every_action_in_the_vocabulary_has_a_button_sentence():
    for code, pair in C.ACTIONS.items():
        label, meaning = pair
        assert label and meaning, code
        assert "_" not in label, f"{code} shows an identifier to the operator"


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
