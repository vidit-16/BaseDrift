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
  STEP_UP -> no API call; payout stays pending while we verify
  STEP_UP + recommended_action="reject"
          -> the reject and deactivate calls, both flagged
             requires_human_confirmation. Nothing rejects unattended; see
             decision_engine's rule table.
  BLOCK   -> no rule reaches this any more. Kept because razorpay_actions is
             a mapping from an outcome to endpoints, and deleting the mapping
             would hide what a rejection is rather than prevent one.
"""

import datetime
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from decision_engine import (
    Decision, VendorRecord, AccountRecord, ALLOW, STEP_UP, BLOCK,
)

CONFIRMED     = "CONFIRMED"       # channel 1: the vendor answered and confirmed
CONTROL_PROVEN = "CONTROL_PROVEN"  # channel 2: they still control the old account
REJECTED      = "REJECTED"
UNREACHABLE   = "UNREACHABLE"
CONTESTED     = "CONTESTED"        # channel 1 says yes, channel 2 says no

UNAVAILABLE_C2 = "CHANNEL_2_UNAVAILABLE"   # no account qualifies to prove control

MAX_CALLBACK_ATTEMPTS = 2

# How long an account must have been on file before it can be used to PROVE
# anything. A tunable with a trade-off, not a discovered constant: longer is
# safer against a planted account and harder on a vendor whose genuine second
# account is recent.
#
# 90 because it has to exceed a normal payment cycle. An account typically
# receives its first settled payout 30-60 days after being added, so a shorter
# window would make "seasoned" and "has a settled payout" the same condition
# asked twice, and the second test would buy nothing.
SEASONING_DAYS = 90


def _days_between(earlier: str, later: str) -> Optional[int]:
    """
    Whole days between two ISO dates, or None if either is missing.

    Deliberately not datetime.now(). Seasoning measured against the clock makes
    the corpus AGE — a case that holds today releases in six months, tests pass
    on the machine that wrote them and fail later, and nothing in the diff
    explains it. Every age here is measured against the REQUEST's own date.
    """
    if not earlier or not later:
        return None
    try:
        a = datetime.date.fromisoformat(earlier)
        b = datetime.date.fromisoformat(later)
    except ValueError:
        return None
    return (b - a).days


def select_verification_account(vendor: VendorRecord,
                                as_of: Optional[str] = None,
                                requested_via: str = "email_request"):
    """
    THE ACCOUNT THE PENNY DROP MUST COME FROM. Returns (AccountRecord, reason)
    or (None, reason).

    The system NAMES the account; the requester never chooses. "Send Rs 1 from
    any account on file" lets an attacker pick the one they planted. "Send Rs 1
    from 434392416664" does not. That sentence is the entire control.

    An account qualifies only if the requester could not have put it there:

      settled_payout_count >= 1   Money actually arrived, so the vendor
                                  controlled it at that moment. Being listed
                                  proves nothing.
      added >= SEASONING_DAYS ago An account added last week is exactly what a
                                  planted one looks like.
      added_via != requested_via  An account added BY AN EMAIL REQUEST cannot
                                  verify another email request. That is
                                  circular, and it is the specific hole: an
                                  attacker who once got an account onto the
                                  master uses a previous success as the
                                  credential for the next one.
      status == active            A closed account cannot send.

    The OLDEST qualifying account wins, not the newest — age is the property
    being relied on.
    """
    if not vendor.accounts:
        return None, "no accounts on file for this vendor"

    qualifying = []
    for a in vendor.accounts:
        if a.status != "active":
            continue
        if a.settled_payout_count < 1:
            continue
        if a.added_via and requested_via and a.added_via == requested_via:
            continue
        age = _days_between(a.added_on, as_of) if as_of else None
        if age is not None and age < SEASONING_DAYS:
            continue
        qualifying.append((age if age is not None else 10 ** 6, a))

    if not qualifying:
        return None, (
            f"no account on file qualifies: none is simultaneously settled, "
            f"at least {SEASONING_DAYS} days old, and added through a channel "
            f"other than the one being verified ({requested_via})")

    qualifying.sort(key=lambda t: -t[0])          # oldest first
    chosen = qualifying[0][1]
    return chosen, (
        f"{chosen.account_number}: {chosen.settled_payout_count} settled "
        f"payout(s), added {chosen.added_on} via {chosen.added_via}, verified "
        f"by {chosen.verified_by or 'unrecorded'}")


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
    # WHICH account the penny drop was demanded from. This is the control, so
    # it belongs in the audit record: "we verified control of an account" and
    # "we verified control of account 4343...6664, which had received eleven
    # settled payouts and was added at onboarding" are different claims.
    verification_account: Optional[str] = None
    verification_account_basis: str = ""
    timestamp:       float = field(default_factory=time.time)

    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


def verify(decision: Decision,
           vendor: VendorRecord,
           callback_reaches_known_contact: bool,
           case_id: str = "UNKNOWN",
           requester_controls_accounts=None,
           as_of: Optional[str] = None,
           requested_via: str = "email_request"
           ) -> Optional[VerificationResult]:
    """
    Runs the two channels (see _run_channels below for how, and why they are
    not equal), then applies one rule on top of the result:

    A HOLD THAT CARRIES A REJECTION RECOMMENDATION IS NEVER AUTO-RELEASED.

    This is what makes removing automatic rejection safe rather than merely
    softer. R2c, R3 and R4 used to end the case themselves; now they hold and
    recommend, so their cases flow into verification for the first time — and
    without this rule a passing channel would RELEASE a case the previous
    version rejected. Recall would fall, quietly, and the release would look
    like a normal verified change in the audit.

    It matters most against the attack V2.6 describes: an attacker who once got
    an account onto the vendor master penny-drops from it and satisfies channel
    2. Evidence of impersonation, an identity conflict against the master, or a
    destination belonging to a different vendor are not things a phone call or a
    rupee can clear. A human decides, with the channel results in front of them.
    """
    res = _run_channels(decision, vendor, callback_reaches_known_contact,
                        case_id, requester_controls_accounts, as_of,
                        requested_via)
    if res is None or not res.payout_allowed:
        return res
    if decision.recommended_action != "reject":
        return res

    return VerificationResult(
        verification_id=res.verification_id,
        outcome=res.outcome,
        final_outcome=STEP_UP,
        contact_used=res.contact_used,
        contact_source=res.contact_source,
        attempts=res.attempts,
        escalated=True,
        simulated=res.simulated,
        simulation_basis=res.simulation_basis,
        verification_account=res.verification_account,
        verification_account_basis=res.verification_account_basis,
        reason=(f"{res.reason} That is not enough here: {decision.rule_fired} "
                f"recommends rejecting this payout, and a verification pass "
                f"cannot overturn it — held for a human, who has both this "
                f"result and the rule's evidence. Original finding: "
                f"{decision.reason[:160]}"),
        payout_allowed=False,
    )


def _run_channels(decision: Decision,
                  vendor: VendorRecord,
                  callback_reaches_known_contact: bool,
                  case_id: str = "UNKNOWN",
                  requester_controls_accounts=None,
                  as_of: Optional[str] = None,
                  requested_via: str = "email_request"
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

    WHICH ACCOUNT, AND WHY IT IS NOT THE REQUESTER'S CHOICE
    =======================================================
    This used to take one bool: "can they send from the account on file". That
    was unambiguous only because every vendor had exactly one account. With
    several, "the account on file" is not a thing, and the premise breaks
    SILENTLY: ask for proof from any account on file and an attacker who once
    got an account added penny-drops from that one. The strongest control in
    the system then confirms the fraud — a previous success used as the
    credential for the next one.

    So select_verification_account() NAMES the account, and this checks whether
    the requester controls THAT one. requester_controls_accounts is what they
    can actually send from; None means channel 2 was not attempted at all, and
    channel 1 decides alone as it did before.

    THE THIRD STATE, which is the part that needs care. When no account
    qualifies, the instinct is to report "not attempted" and let the callback
    decide. THAT IS WRONG: the callback is what sim-swap defeats, so a vendor
    with no seasoned account and a compromised phone would be RELEASED. Channel
    2 unavailable therefore escalates and never falls back — the same rule as
    everywhere else here, that "could not check" is not "checked and clean".

    It happens legitimately, for a genuinely new vendor with no payment history,
    and holding is correct there: nothing exists to compare against.

    THE HONEST LIMIT. A patient attacker plants an account, waits out the
    seasoning window, lets it receive one real payout, and then uses it. That
    defeats this. But it requires them to have ALREADY SUCCEEDED ONCE, so this
    is containment rather than prevention: it stops one compromise
    bootstrapping the next, which is the realistic threat. The channel is not
    airtight and is not claimed to be.
    """
    if decision.outcome != STEP_UP:
        return None

    vid = f"VER-{case_id}-{uuid.uuid4().hex[:6].upper()}"
    contact = vendor.known_phone

    # ── Channel 2, when it was attempted ──────────────────────────────
    if requester_controls_accounts is not None:
        named, basis = select_verification_account(vendor, as_of, requested_via)

        if named is None:
            # THE THIRD STATE. Not clean, not failed — unavailable. It must not
            # fall through to the callback below, because that is the channel
            # sim-swap defeats.
            return VerificationResult(
                verification_id=vid,
                outcome=UNAVAILABLE_C2,
                final_outcome=STEP_UP,
                contact_used=contact,
                contact_source="vendor_master",
                attempts=1,
                escalated=True,
                simulated=True,
                simulation_basis=f"channel 2 unavailable — {basis}",
                verification_account=None,
                verification_account_basis=basis,
                reason=(f"No account on file can be used to prove control: "
                        f"{basis}. Proof of control is the authoritative "
                        f"channel, so its absence escalates rather than "
                        f"deferring to the callback — a number can be ported, "
                        f"and falling back here would release exactly the case "
                        f"the second channel exists for. Held for a human."),
                payout_allowed=False,
            )

        controlled = set(requester_controls_accounts)

        if named.account_number in controlled:
            return VerificationResult(
                verification_id=vid,
                outcome=CONTROL_PROVEN,
                final_outcome=ALLOW,
                contact_used=named.account_number,
                contact_source="vendor_master",
                attempts=1,
                escalated=False,
                simulated=True,
                simulation_basis=f"a penny drop from the NAMED account "
                                 f"{named.account_number} completed",
                verification_account=named.account_number,
                verification_account_basis=basis,
                reason=f"Reverse Penny Drop from {named.account_number} — an "
                       f"account the requester could not have planted ({basis}) "
                       f"— completed. They still control where money has been "
                       f"going, which someone who has only taken the mailbox "
                       f"and the phone cannot do.",
                payout_allowed=True,
            )

        # Channel 2 failed. Channel 1 cannot overrule it: channel 1 passing
        # while channel 2 fails is exactly SIM-swap fraud, and it is
        # indistinguishable from a vendor who closed their old bank account.
        planted = bool(controlled)
        return VerificationResult(
            verification_id=vid,
            outcome=CONTESTED if callback_reaches_known_contact else UNREACHABLE,
            final_outcome=STEP_UP,
            contact_used=contact,
            contact_source="vendor_master",
            attempts=MAX_CALLBACK_ATTEMPTS,
            escalated=True,
            simulated=True,
            simulation_basis=f"no penny drop from the named account "
                             f"{named.account_number}",
            verification_account=named.account_number,
            verification_account_basis=basis,
            reason=(
                f"No payment could be made from {named.account_number}, the "
                f"account this system named ({basis}). "
                + (f"The requester CAN send from another account on file, which "
                   f"is not the same thing and is not accepted: an account they "
                   f"could have had added is not evidence they are the vendor. "
                   if planted else "")
                + (f"The callback to {contact} was answered, which on its own is "
                   f"what a taken-over number looks like — and also what a "
                   f"genuinely closed account looks like. Held for a human."
                   if callback_reaches_known_contact else
                   f"{MAX_CALLBACK_ATTEMPTS} callbacks to {contact} also went "
                   f"unanswered. Held and escalated.")
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
                     fund_account_id: str = "fa_TEST",
                     recommended_action: Optional[str] = None
                     ) -> List[Dict[str, Any]]:
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

    if recommended_action == "reject":
        # A hold the engine believes should end in a rejection. Emitted so a
        # reviewer sees the recommendation and can act on it in one click — but
        # flagged, so nothing executes it unattended. Until someone does, the
        # payout stays pending, which is the safe state either way.
        return [
            {
                "method": "POST",
                "endpoint": f"/v1/payouts/{payout_id}/reject",
                "body": {"remarks": remarks},
                "effect": "RECOMMENDED — payout stays pending until a human agrees",
                "requires_human_confirmation": True,
            },
            {
                "method": "PATCH",
                "endpoint": f"/v1/fund_accounts/{fund_account_id}",
                "body": {"active": False},
                "effect": "RECOMMENDED — destination disabled only on confirmation",
                "requires_human_confirmation": True,
            },
        ]

    return [{
        "method": None,
        "endpoint": None,
        "body": None,
        "effect": "no API call — payout stays pending while verification runs",
    }]
