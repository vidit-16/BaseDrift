"""
PayeeProof — verifier.py

Handles the STEP_UP_VERIFY path and emits the RazorpayX API actions
a decision maps to.

THE CORE RULE, stated in code and not only in docs:
the callback goes to vendor.known_phone — the number already on file —
never to any number that appeared in the request. An attacker who
supplied a phone number in a spoofed email cannot intercept it.

BOUNDED HOLD
Razorpay auto-rejects any payout left pending beyond ~3 months, so an
unresolved hold cannot sit forever. We escalate well before that:
2 callback attempts, then human escalation.

API ACTIONS EMITTED (real RazorpayX endpoints)
  ALLOW   -> POST  /v1/payouts/{id}/approve   {"remarks": ...}
  BLOCK   -> POST  /v1/payouts/{id}/reject    {"remarks": ...}
           + PATCH /v1/fund_accounts/{id}     {"active": false}
  STEP_UP -> no API call; payout stays pending while we verify
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from decision_engine import Decision, VendorRecord, ALLOW, STEP_UP, BLOCK

CONFIRMED   = "CONFIRMED"
REJECTED    = "REJECTED"
UNREACHABLE = "UNREACHABLE"

MAX_CALLBACK_ATTEMPTS = 2


@dataclass
class VerificationResult:
    verification_id: str
    outcome:         str
    final_outcome:   str
    contact_used:    str
    contact_source:  str          # always "vendor_master"
    attempts:        int
    escalated:       bool
    simulated:       bool
    simulation_basis: str
    reason:          str
    payout_allowed:  bool = False
    timestamp:       float = field(default_factory=time.time)

    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


def verify(decision: Decision,
           vendor: VendorRecord,
           callback_reaches_known_contact: bool,
           case_id: str = "UNKNOWN") -> Optional[VerificationResult]:
    """
    Only STEP_UP decisions route here. Returns None otherwise.

    callback_reaches_known_contact comes from the scenario ground truth
    and represents whether the REAL vendor answers — an attacker never
    controls this number, which is the entire point of the control.
    """
    if decision.outcome != STEP_UP:
        return None

    vid = f"VER-{case_id}-{uuid.uuid4().hex[:6].upper()}"
    contact = vendor.known_phone

    if callback_reaches_known_contact:
        return VerificationResult(
            verification_id=vid,
            outcome=CONFIRMED,
            final_outcome=ALLOW,
            contact_used=contact,
            contact_source="vendor_master",
            attempts=1,
            escalated=False,
            simulated=True,
            simulation_basis="callback_reaches_known_contact=True — the vendor "
                             "answers on the number already on file and confirms",
            reason=f"Callback to {contact} (from vendor master) reached the vendor. "
                   f"Change confirmed through a channel the requester does not control.",
            payout_allowed=True,
        )

    return VerificationResult(
        verification_id=vid,
        outcome=UNREACHABLE,
        final_outcome=STEP_UP,
        contact_used=contact,
        contact_source="vendor_master",
        attempts=MAX_CALLBACK_ATTEMPTS,
        escalated=True,
        simulated=True,
        simulation_basis="callback_reaches_known_contact=False — nobody answers "
                         "on the number on file, which is expected when the "
                         "requester is not the vendor",
        reason=f"{MAX_CALLBACK_ATTEMPTS} callback attempts to {contact} "
               f"(from vendor master) went unanswered. Payout remains held and "
               f"is escalated for human review. Never auto-released.",
        payout_allowed=False,
    )


def razorpay_actions(outcome: str,
                     reason: str,
                     payout_id: str = "pout_TEST",
                     fund_account_id: str = "fa_TEST") -> List[Dict[str, Any]]:
    """
    The real RazorpayX calls a decision maps to. Returned as data so the
    pipeline can log them; execution is a separate, explicit step.

    Endpoints:
      POST  /v1/payouts/{id}/approve
      POST  /v1/payouts/{id}/reject
      PATCH /v1/fund_accounts/{id}   {"active": false}
    """
    remarks = f"PayeeProof: {reason[:180]}"

    if outcome == ALLOW:
        return [{
            "method": "POST",
            "endpoint": f"/v1/payouts/{payout_id}/approve",
            "body": {"remarks": remarks},
            "effect": "payout released from pending",
        }]

    if outcome == BLOCK:
        return [
            {
                "method": "POST",
                "endpoint": f"/v1/payouts/{payout_id}/reject",
                "body": {"remarks": remarks},
                "effect": "payout rejected while still pending — no money moved",
            },
            {
                "method": "PATCH",
                "endpoint": f"/v1/fund_accounts/{fund_account_id}",
                "body": {"active": False},
                "effect": "fund account deactivated so it cannot be reused",
            },
        ]

    return [{
        "method": None,
        "endpoint": None,
        "body": None,
        "effect": "no API call — payout stays pending while verification runs",
    }]
