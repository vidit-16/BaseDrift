"""
PayeeProof — verifier.py

Handles the STEP_UP_VERIFY path and emits the RazorpayX API actions
a decision maps to.

THE CORE RULE, stated in code and not only in docs:
the callback goes to vendor.known_phone — the number already on file —
never to any number that appeared in the request. An attacker who
supplied a phone number in a spoofed email cannot intercept it.

SECOND CHANNEL
A phone number can be taken. The account we have been paying cannot — moving
money away from it is the entire point of the attack. So a Reverse Penny Drop
from the EXISTING account is the authoritative channel, and the callback
corroborates. See verify() for why "either channel passes" is the wrong rule.

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

CONFIRMED     = "CONFIRMED"       # channel 1: the vendor answered and confirmed
CONTROL_PROVEN = "CONTROL_PROVEN"  # channel 2: they still control the old account
REJECTED      = "REJECTED"
UNREACHABLE   = "UNREACHABLE"
CONTESTED     = "CONTESTED"        # channel 1 says yes, channel 2 says no

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
           case_id: str = "UNKNOWN",
           controls_existing_account: Optional[bool] = None
           ) -> Optional[VerificationResult]:
    """
    Only STEP_UP decisions route here. Returns None otherwise.

    TWO CHANNELS, AND THEY ARE NOT EQUAL
    ====================================
    channel 1 — call the number in the vendor master. Proves a human on the
                vendor's known phone says yes.
    channel 2 — Reverse Penny Drop from the account ALREADY ON FILE. Proves the
                requester still controls where money has been going.

    "Either one passes" was the obvious design and it is wrong. SIM-swap fraud
    has channel 1 PASSING — the attacker holds the phone and answers it — so an
    either/or rule releases exactly the cases the second channel was added for.

    Channel 2 is therefore authoritative and channel 1 corroborating, mirroring
    the Tier 1 / Tier 2 split in the rule table. The asymmetry is real: taking
    over a mailbox or porting a number is far easier than gaining access to the
    victim's bank account, and redirecting money AWAY from that account is the
    whole point of the attack, so an attacker cannot send from it.

    The honest cost: a vendor who genuinely closed their old account fails
    channel 2 through no fault of their own, and lands in CONTESTED alongside
    the attacker. Those two are indistinguishable on evidence, so both are held
    for a human rather than released. A hold is recoverable; a release is not.

    controls_existing_account=None means channel 2 was not attempted — RPD is
    enabled on request rather than by default — and channel 1 decides alone, as
    it did before.
    """
    if decision.outcome != STEP_UP:
        return None

    vid = f"VER-{case_id}-{uuid.uuid4().hex[:6].upper()}"
    contact = vendor.known_phone

    # ── Channel 2, when it was attempted ──────────────────────────────
    # KNOWN LIMIT (NOTES.md V2.6): this treats "the account already on file" as
    # unambiguous, which it is only because every vendor currently has exactly
    # one. Once a vendor has several, the caller must NAME which account the
    # penny drop had to come from — and it has to be one the requester could not
    # have planted: settled-payout history, seasoned, and not added by the same
    # channel now being verified. Otherwise an attacker who got a second account
    # onto the master drops from THAT one and this channel confirms their fraud.
    if controls_existing_account is True:
        return VerificationResult(
            verification_id=vid,
            outcome=CONTROL_PROVEN,
            final_outcome=ALLOW,
            contact_used=vendor.known_account_number,
            contact_source="vendor_master",
            attempts=1,
            escalated=False,
            simulated=True,
            simulation_basis="controls_existing_account=True — a penny drop from "
                             "the account already on file completed",
            reason=f"Reverse Penny Drop from {vendor.known_account_number} (the "
                   f"account already on file) completed. The requester still "
                   f"controls where money has been going — which someone who has "
                   f"only taken the mailbox and the phone cannot do.",
            payout_allowed=True,
        )

    if controls_existing_account is False:
        # Channel 2 failed. Channel 1 cannot overrule it: the case where channel
        # 1 passes and channel 2 fails is exactly SIM-swap fraud, and it is
        # indistinguishable from a vendor who closed their old bank account.
        return VerificationResult(
            verification_id=vid,
            outcome=CONTESTED if callback_reaches_known_contact else UNREACHABLE,
            final_outcome=STEP_UP,
            contact_used=contact,
            contact_source="vendor_master",
            attempts=MAX_CALLBACK_ATTEMPTS,
            escalated=True,
            simulated=True,
            simulation_basis="controls_existing_account=False — no penny drop "
                             "from the account on file",
            reason=(
                f"The callback to {contact} was answered, but no payment could be "
                f"made from {vendor.known_account_number}, the account already on "
                f"file. Someone confirming by phone while unable to send from the "
                f"existing account is what a taken-over number looks like — and is "
                f"also what a genuinely closed account looks like. Held for a human."
                if callback_reaches_known_contact else
                f"Neither channel completed: {MAX_CALLBACK_ATTEMPTS} callbacks to "
                f"{contact} went unanswered and no payment came from "
                f"{vendor.known_account_number}. Held and escalated."
            ),
            payout_allowed=False,
        )

    # ── Channel 2 not attempted: channel 1 alone, as before ───────────
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

    NOTHING IN THIS REPOSITORY CALLS RAZORPAY. These are action plans. Any
    action carrying requires_human_confirmation must not be executed
    automatically by whatever eventually does run them.

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
                # Rejecting one payout is recoverable; deactivating the fund
                # account affects every future payout to it and cannot be undone
                # from here. At the measured false-block rate that is roughly one
                # legitimate vendor in 170 losing a destination on a decision no
                # human reviewed. The reject stands alone; this waits for review.
                "requires_human_confirmation": True,
            },
        ]

    return [{
        "method": None,
        "endpoint": None,
        "body": None,
        "effect": "no API call — payout stays pending while verification runs",
    }]
