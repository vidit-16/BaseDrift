"""
PayeeProof — the case file: what a human did about a held payout.

The decision engine says *hold, and here is what would release this*. Everything
after that happens in the physical world: somebody rings the supplier on a number
the supplier did not supply, or watches for a rupee arriving from a named
account. This module is where those outside-the-system facts are recorded, and
where the rules about who may record what are enforced.

TWO THINGS THIS FILE EXISTS TO GET RIGHT
========================================

**1. The log is the truth; the state is a fold over it.**
There is no `case.status` field to be written to twice, or to drift from the
history that supposedly produced it. `state_of()` recomputes from the recorded
actions every time. That costs nothing at this size and means a case can never
claim a status its own audit trail does not support.

**2. Segregation of duties is a rule, not a greyed-out button.**
Whoever records a verification outcome may not release the payment. The reason
is the entire threat model: an AP clerk who is compromised, coerced or complicit
can type "supplier confirmed by phone" and then release to the attacker's
account, and every control upstream becomes theatre. A disabled button stops an
honest mistake. A server-side check stops the attack — so `may_release()` is
authoritative and the UI merely reflects it.

The counterpart is deliberate: **rejection is not segregated.** The two-person
rule protects money leaving; refusing to pay releases nothing. Requiring a second
person to reject would slow down the safe direction and add nothing, so the
control is applied where the loss is.

WHAT THIS IS NOT
It is an in-memory record for the demo and for evaluation. Production needs it
durable, authenticated and append-only, alongside everything else in
COMPLIANCE.md. The rules here would not change; only their storage would.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

# ── Who is on the desk ───────────────────────────────────────
#
# Three named roles, because the two-person rule is invisible until there are
# two people. Not authentication — the demo has none, and COMPLIANCE.md says so
# — but enough that the control can be exercised rather than described.

OPERATORS = [
    ("Priya Menon",  "Accounts payable"),
    ("Rahul Iyer",   "Financial controller"),
    ("Meera Nair",   "Fraud control"),
]


# ── The vocabulary of things a human can record ──────────────────────
#
# Each entry: the sentence shown on the button, and what recording it means for
# the case. Verification outcomes are separated from resolutions because only
# the first group triggers the two-person rule.

CALLBACK_OUTCOMES = {
    "callback_confirmed": (
        "Supplier confirmed the change",
        "Reached them on the number in our records and they confirmed it."),
    "callback_denied": (
        "Supplier says they did not send this",
        "They deny sending the request. Treat the message as fraudulent."),
    "callback_contested": (
        "Reached them, but something did not add up",
        "The call happened and left doubt rather than settling it."),
    "callback_unreachable": (
        "Could not reach the supplier",
        "No answer on the number we hold. Nothing is confirmed either way."),
}

PROOF_OUTCOMES = {
    "proof_received": (
        "Rs 1 received from the named account",
        "The supplier proved they control an account we already trust."),
    "proof_not_received": (
        "Rs 1 never arrived",
        "They could not send from the account we asked for."),
}

VERIFICATION_ACTIONS = {**CALLBACK_OUTCOMES, **PROOF_OUTCOMES}

REQUEST_ACTIONS = {
    "callback_requested": (
        "Log a callback attempt",
        "Records that the call is being made, before its outcome is known."),
    "proof_requested": (
        "Ask for Rs 1 from the named account",
        "Records the demand, so the account asked for is on the file."),
}

RESOLUTIONS = {
    "released": ("Release the payment",
                 "The payout proceeds to the destination on the event."),
    "rejected": ("Reject the payment",
                 "The payout is refused. No money moves."),
}

ACTIONS = {**VERIFICATION_ACTIONS, **REQUEST_ACTIONS, **RESOLUTIONS}

# ── The states a case can be in ──────────────────────────────────────

STATE_LABEL = {
    "released":       "Released",
    "rejected":       "Rejected",
    "verified":       "Verified — ready for a second person",
    "contested":      "Verification failed",
    "awaiting_proof": "Waiting for Rs 1 from the named account",
    "contacting":     "Callback in progress",
    "open":           "Held — nothing recorded yet",
    "no_action":      "Released automatically — no case to work",
}

# What an operator should do next, per state. The queue is worthless if it shows
# status and not the next move.
STATE_NEXT = {
    "open":
        "Nothing has been recorded. Call the supplier on the number in our "
        "records — never a number in the request — or ask for the rupee.",
    "contacting":
        "A call is logged as under way. Record what the supplier said.",
    "awaiting_proof":
        "The demand is on file. Record whether the rupee arrived.",
    "verified":
        "Verified. A second person — not whoever verified it — releases the "
        "payment.",
    "contested":
        "Verification did not hold up. This should be rejected, and the "
        "supplier contacted through a channel they did not choose.",
}


def _who(actor: Any) -> str:
    """
    The identity two records are compared on.

    Case and padding are presentation. "Priya" and " priya " are one person,
    and letting them be two would defeat segregation of duties with a shift key.
    The name is still stored as typed — this is only the comparison.
    """
    return str(actor or "").strip().casefold()


def record(actions: List[Dict[str, Any]], action: str, actor: str,
           note: str = "", detail: str = "",
           at: Optional[float] = None) -> Dict[str, Any]:
    """Append one recorded fact. Rejects anything not in the vocabulary."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if not (actor or "").strip():
        raise ValueError("every recorded action needs a named person")
    entry = {
        "action": action,
        "actor": actor.strip(),
        "note": (note or "").strip(),
        "detail": (detail or "").strip(),
        "at": time.time() if at is None else at,
    }
    actions.append(entry)
    return entry


def state_of(actions: List[Dict[str, Any]],
             final_outcome: Optional[str] = None) -> str:
    """
    The case's state, recomputed from its own history every time.

    Order matters: a resolution ends the case, a failed verification outbids a
    successful earlier one (a supplier who confirms and then denies is a case
    for the fraud team, not a release), and a request only stands until its
    outcome is recorded.
    """
    if not actions:
        return "no_action" if final_outcome == "ALLOW" else "open"

    codes = [a.get("action") for a in actions]
    for terminal in ("released", "rejected"):
        if terminal in codes:
            return terminal

    # A negative verification is sticky. Once a supplier has denied the request
    # or failed to send the rupee, a later "confirmed" does not wash it out —
    # that sequence is itself the thing worth escalating.
    if any(c in ("callback_denied", "callback_contested", "proof_not_received")
           for c in codes):
        return "contested"
    if any(c in ("callback_confirmed", "proof_received") for c in codes):
        return "verified"
    if "proof_requested" in codes:
        return "awaiting_proof"
    if "callback_requested" in codes:
        return "contacting"
    return "open"


def verifiers(actions: List[Dict[str, Any]]) -> List[str]:
    """Everyone who recorded a verification outcome on this case."""
    seen: List[str] = []
    for a in actions:
        if a.get("action") in VERIFICATION_ACTIONS:
            actor = _who(a.get("actor"))
            if actor and actor not in seen:
                seen.append(actor)
    return seen


def may_release(actions: List[Dict[str, Any]], actor: str,
                final_outcome: Optional[str] = None) -> Tuple[bool, str]:
    """
    Whether this person may release this payment, and if not, why not.

    Three refusals, in the order they are checked:

    1. The case is already resolved — releasing twice is not a thing.
    2. Nothing has been verified. Fail-safe by inaction: a held payment stays
       held until somebody establishes something, not until somebody grows
       tired of it.
    3. This person recorded the verification. That is the two-person rule, and
       it is checked here rather than in the template because a control that
       lives only in the UI is not a control.
    """
    state = state_of(actions, final_outcome)
    if state in ("released", "rejected"):
        return False, f"This case is already {STATE_LABEL[state].lower()}."
    if state == "contested":
        return False, ("Verification did not hold up. This cannot be released "
                       "on the strength of the checks recorded.")
    if state != "verified":
        return False, ("Nothing is verified yet. Record a callback outcome or "
                       "the rupee arriving before releasing.")
    if _who(actor) in verifiers(actions):
        return False, ("You recorded the verification on this case, so you "
                       "cannot also release it. A different person must.")
    return True, ""


def may_reject(actions: List[Dict[str, Any]],
               final_outcome: Optional[str] = None) -> Tuple[bool, str]:
    """
    Rejection is open to anyone, at any point, and needs no verification.

    Refusing to pay moves no money, so the two-person rule buys nothing here
    and would only slow down the safe direction. The engine still never rejects
    on its own — that remains a human act, which is the whole point of the
    recommendation being a recommendation.
    """
    state = state_of(actions, final_outcome)
    if state in ("released", "rejected"):
        return False, f"This case is already {STATE_LABEL[state].lower()}."
    return True, ""


def summary(actions: List[Dict[str, Any]],
            final_outcome: Optional[str] = None) -> Dict[str, Any]:
    """Everything the dashboard needs about a case, computed in one place."""
    state = state_of(actions, final_outcome)
    return {
        "state": state,
        "label": STATE_LABEL.get(state, state),
        "next_step": STATE_NEXT.get(state, ""),
        "verifiers": verifiers(actions),
        "resolved": state in ("released", "rejected"),
        "actions": list(actions),
    }
