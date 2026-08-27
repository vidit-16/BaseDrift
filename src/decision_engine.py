"""
PayeeProof — decision_engine.py

Turns extraction output + FAV result + vendor master into a Decision.
No LLM here. Every rule below is explicit and traceable.

WHY THIS EXISTS
Razorpay's FAV compares the bank's registered name against "the name
provided by the customer" — a field the requester controls. Reverse
Penny Drop proves the account holder's identity, but an attacker who
owns the destination account satisfies it. Neither establishes that
the legitimate vendor AUTHORIZED this change. That is what we check.

═══════════════════════════════════════════════════════════════════════
RULE TABLE — authoritative. Code below implements exactly this.
═══════════════════════════════════════════════════════════════════════

WHICH ACCOUNT GETS CHECKED
The payout's real fund account, resolved from RazorpayX at payout.pending, is
authoritative. The account number the LLM read out of the email is only a CLAIM
and is used solely as a fallback for offline analysis, flagged as such. Checking
the claim instead of the destination would let a request describe one account
while the money went to another.

TIER 1 — identity binding (against vendor master, never against request)

  name_match_score (from FAV):
    >= 85       PASS
    60-84       WARN    graduated: "Pvt Ltd" vs "Private Limited" etc.
    < 60        FAIL

  account_status (from FAV):
    active      PASS
    inactive    FAIL    the bank says this account cannot receive
    unknown     WARN    FAV inconclusive — not the same as clean

  gstin:
    matches, not hedged     PASS
    matches, hedged         WARN    weaker claim
    absent                  WARN
    mismatch                FAIL
  Hedge detection matches the CONCEPT in hedged_fields, not an exact string.
  The model emits "gstin", "proposed_gstin", "gst_number" for one idea; an
  exact-match tuple silently missed hedges and therefore failed OPEN.

  account_continuity:            (on the resolved destination, see above)
    same as a known account          PASS
    new account                      WARN    legitimate changes exist
    account seen on another vendor   FAIL    cross-contact reuse
    no destination resolvable        WARN    cannot verify != verified

TIER 2 — contextual signals (supporting evidence, never decisive alone)

  sender_domain:          match PASS / mismatch WARN
  urgency:                absent PASS / present WARN
  channel_manipulation:   absent PASS / present WARN
  payment_pattern:        within 15% of avg and no flags PASS, else WARN

═══════════════════════════════════════════════════════════════════════
DECISION POLICY — first match wins
═══════════════════════════════════════════════════════════════════════

  1. extraction failed                          STEP_UP
     "we couldn't check" != "we found fraud"

  2. intent == PAYMENT_FOLLOWUP — the request claims nothing is changing.
     VERIFY that claim against the resolved destination; do not take it.
     2a. destination is a known account          ALLOW
         claim corroborated; there is genuinely nothing to authorize
     2b. destination unknown / unresolvable      STEP_UP
         the layer says "no change" while the money goes somewhere new,
         or we cannot tell where it goes — a contradiction, not a clearance
     2c. destination belongs to another vendor   BLOCK

     Rule 2 used to return ALLOW on the intent label alone, before any
     identity check ran. That made a single LLM output sufficient to release
     a payout, and made a prompt injection that reached PAYMENT_FOLLOWUP a
     complete bypass. The semantic layer is evidence; it is not authority.
     Only continuity runs here — a follow-up requests no change, so the one
     thing that must hold is that the destination really is unchanged.
     Running the full battery would step up every routine follow-up.

  3. any Tier 1 FAIL                            BLOCK

  4. REPLACE + new account + >=2 Tier 2 WARN    BLOCK
     the BEC pattern: cut off the old destination, under pressure,
     from an unverified channel

  5. any Tier 1 WARN                            STEP_UP

  6. any Tier 2 WARN                            STEP_UP

  7. all PASS                                   ALLOW

WHY ACTION MATTERS (rule 4)
RazorpayX permits multiple fund accounts per contact, so a new account
is not inherently a replacement. ADD keeps the existing destination
alive — materially lower risk than REPLACE, which severs it. Treating
both identically would either miss real fraud or block legitimate
multi-account vendors.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from extractor import (
    ExtractionResult,
    INTENT_CHANGE, INTENT_FOLLOWUP,
    ACTION_REPLACE, ACTION_ADD, ACTION_NONE,
)

# ── Outcomes ──────────────────────────────────────────────────────────

ALLOW   = "ALLOW"
STEP_UP = "STEP_UP_VERIFY"
BLOCK   = "BLOCK"

# ── Thresholds (single place to tune) ─────────────────────────────────

NAME_SCORE_PASS  = 85
NAME_SCORE_WARN  = 60
AMOUNT_TOLERANCE = 0.15

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


# ── Data ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    name:   str
    tier:   int
    result: str      # PASS | WARN | FAIL
    detail: str
    source: str      # provenance: where the evidence came from


@dataclass
class FAVResult:
    """
    Schema-faithful replay of Razorpay's Fund Account Validation.
    Production: razorpay.com/docs/x/fund-account-validation/
    Test mode:  FAV is unavailable, so values come from the scenario.
    """
    account_status:   str            # active | inactive | unknown
    registered_name:  Optional[str]
    name_match_score: Optional[int]  # None = validation unavailable


@dataclass
class VendorRecord:
    vendor_id:            str
    legal_name:           str
    gstin:                str
    known_domain:         str
    known_phone:          str
    known_account_number: str
    known_ifsc:           str
    avg_payout_amount:    float
    # Multiple fund accounts per contact are permitted by RazorpayX
    additional_accounts:  List[str] = field(default_factory=list)

    def all_known_accounts(self) -> List[str]:
        return [self.known_account_number] + list(self.additional_accounts)


@dataclass
class Decision:
    outcome:        str
    reason:         str
    rule_fired:     str
    triggered_by:   List[str]
    tier1:          List[Signal] = field(default_factory=list)
    tier2:          List[Signal] = field(default_factory=list)
    payout_allowed: bool = False
    needs_callback: bool = False
    # Provenance of the account actually checked — belongs in the audit trail,
    # since "which account did you validate" is the whole question here.
    checked_destination: Optional[str] = None
    destination_source:  Optional[str] = None

    def to_dict(self):
        return {
            "outcome": self.outcome,
            "rule_fired": self.rule_fired,
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "payout_allowed": self.payout_allowed,
            "needs_callback": self.needs_callback,
            "checked_destination": self.checked_destination,
            "destination_source": self.destination_source,
            "tier1": [vars(s) for s in self.tier1],
            "tier2": [vars(s) for s in self.tier2],
        }


# ── Shared helpers ────────────────────────────────────────────────────

def resolve_destination(ext: ExtractionResult,
                        destination_account_number: Optional[str] = None):
    """
    Returns (account_number, source).

    The payout's own fund account wins whenever we have it — that is where the
    money actually goes. ext.proposed_account_number is only what the message
    CLAIMED, and is used solely as an offline fallback so batch analysis of raw
    emails still works. The source string travels into the audit record so a
    reader can always tell which of the two was validated.
    """
    if destination_account_number:
        return str(destination_account_number).strip(), "razorpay_payout"
    if ext.proposed_account_number:
        return ext.proposed_account_number, "email_claim_only"
    return None, "unresolved"


def _hedged(hedged_fields: List[str], *concepts: str) -> bool:
    """
    True if any hedged field refers to one of `concepts`.

    hedged_fields is free-form model output: "gstin", "proposed_gstin" and
    "gst_number" were all observed for the same idea across runs of one input.
    The previous exact-match tuple missed the spellings it did not enumerate,
    and a missed hedge means one fewer WARN — it failed OPEN. Match the concept
    inside the normalised field name instead.
    """
    for f in hedged_fields or []:
        norm = re.sub(r"[^a-z0-9]", "", str(f).lower())
        if any(c in norm for c in concepts):
            return True
    return False


# ── Tier 1 ────────────────────────────────────────────────────────────

def check_name_match(fav: FAVResult) -> Signal:
    if fav.name_match_score is None:
        return Signal("name_match", 1, WARN,
                      "FAV unavailable — no name evidence. Inconclusive, not clean.",
                      "razorpay_fav")
    s = fav.name_match_score
    if s >= NAME_SCORE_PASS:
        return Signal("name_match", 1, PASS,
                      f"score {s} >= {NAME_SCORE_PASS}", "razorpay_fav")
    if s >= NAME_SCORE_WARN:
        return Signal("name_match", 1, WARN,
                      f"score {s} marginal — could be a legal-name variation",
                      "razorpay_fav")
    return Signal("name_match", 1, FAIL,
                  f"score {s} — conflicts with vendor master", "razorpay_fav")


def check_account_status(fav: FAVResult) -> Signal:
    """
    FAV reports whether the account can actually receive. Previously this field
    was carried on FAVResult, logged, printed — and read by nothing, so an
    inactive account with a 99 name match passed Tier 1 outright.
    """
    st = (fav.account_status or "").strip().lower()
    if st == "active":
        return Signal("account_status", 1, PASS, "account is active", "razorpay_fav")
    if st == "inactive":
        return Signal("account_status", 1, FAIL,
                      "bank reports the account inactive — it cannot receive",
                      "razorpay_fav")
    return Signal("account_status", 1, WARN,
                  f"account status {st or 'missing'!r} — FAV inconclusive, not clean",
                  "razorpay_fav")


def check_gstin(ext: ExtractionResult, v: VendorRecord) -> Signal:
    proposed = ext.proposed_gstin
    if proposed is None:
        return Signal("gstin", 1, WARN, "no GSTIN in request", "email_body")
    if proposed.upper() != v.gstin.upper():
        return Signal("gstin", 1, FAIL,
                      f"mismatch: {proposed} vs master {v.gstin}",
                      "email_body vs vendor_master")
    if _hedged(ext.hedged_fields, "gst"):
        return Signal("gstin", 1, WARN,
                      f"{proposed} matches but the claim was hedged",
                      "email_body vs vendor_master")
    if ext.hedging_detected and not ext.hedged_fields:
        # Hedging was detected but the model did not say where. Treat the GSTIN
        # claim as hedged: the cost is a callback, and "we cannot tell" resolving
        # to WARN is the same fail-safe direction as every other rule here.
        return Signal("gstin", 1, WARN,
                      f"{proposed} matches, but hedging was detected with no field "
                      f"named — treated as hedged",
                      "email_body vs vendor_master")
    return Signal("gstin", 1, PASS, f"{proposed} matches vendor master",
                  "email_body vs vendor_master")


def check_account_continuity(ext: ExtractionResult, v: VendorRecord,
                              other_vendor_accounts: Optional[Dict[str, str]] = None,
                              destination_account_number: Optional[str] = None) -> Signal:
    """
    Checks the account the money will actually reach — see resolve_destination().

    other_vendor_accounts: {account_number: vendor_id} across the whole
    vendor master. RazorpayX allows the same account under multiple
    contacts, which is legitimate for corporate groups but is also
    exactly how one attacker redirects several vendors to one destination.
    """
    dest, src = resolve_destination(ext, destination_account_number)

    if dest is None:
        return Signal("account_continuity", 1, WARN,
                      "no destination could be resolved — neither the payout's "
                      "fund account nor an account number in the request",
                      "unresolved")

    # Provenance is stated in every detail string: a reader must be able to tell
    # a verified destination from a self-reported one at a glance.
    prov = ("payout fund account" if src == "razorpay_payout"
            else "UNVERIFIED — from the request, no payout destination supplied")

    if other_vendor_accounts:
        owner = other_vendor_accounts.get(dest)
        if owner and owner != v.vendor_id:
            return Signal("account_continuity", 1, FAIL,
                          f"account {dest} ({prov}) is already on file for a "
                          f"different vendor ({owner}) — cross-contact reuse",
                          "vendor_master_crosscheck")

    if dest in v.all_known_accounts():
        return Signal("account_continuity", 1, PASS,
                      f"{dest} ({prov}) is a known account for this vendor",
                      f"{src} vs vendor_master")

    return Signal("account_continuity", 1, WARN,
                  f"new account {dest} ({prov}) — known: {v.all_known_accounts()}",
                  f"{src} vs vendor_master")


# ── Tier 2 ────────────────────────────────────────────────────────────

def check_domain(ext: ExtractionResult, v: VendorRecord) -> Signal:
    d = ext.sender_domain
    if d is None:
        return Signal("sender_domain", 2, WARN, "sender domain not found", "email_header")
    if d.lower() == v.known_domain.lower():
        return Signal("sender_domain", 2, PASS, f"{d} matches known domain",
                      "email_header vs vendor_master")
    return Signal("sender_domain", 2, WARN,
                  f"{d} != known {v.known_domain}",
                  "email_header vs vendor_master")


def check_urgency(ext: ExtractionResult) -> Signal:
    if not ext.urgency_detected:
        return Signal("urgency", 2, PASS, "no urgency language", "semantic_layer")
    phrases = "; ".join(ext.urgency_phrases[:2]) or "detected"
    return Signal("urgency", 2, WARN, f"urgency: {phrases}", "semantic_layer")


def check_channel_manipulation(ext: ExtractionResult) -> Signal:
    if not ext.channel_manipulation_detected:
        return Signal("channel_manipulation", 2, PASS,
                      "no channel redirection", "semantic_layer")
    phrases = "; ".join(ext.channel_manipulation_phrases[:2]) or "detected"
    return Signal("channel_manipulation", 2, WARN,
                  f"redirecting communication: {phrases}", "semantic_layer")


def check_payment_pattern(ext: ExtractionResult, v: VendorRecord,
                           near_duplicate=False, split_below=False) -> Signal:
    if ext.amount is None:
        return Signal("payment_pattern", 2, WARN, "no amount found", "email_body")

    flags = []
    if split_below:
        flags.append("split below approval threshold")
    if near_duplicate:
        flags.append("near-duplicate invoice")
    if flags:
        return Signal("payment_pattern", 2, WARN, "; ".join(flags), "case_metadata")

    if not v.avg_payout_amount:
        return Signal("payment_pattern", 2, PASS, "no baseline to compare", "vendor_master")

    dev = abs(1.0 - ext.amount / v.avg_payout_amount)
    if dev <= AMOUNT_TOLERANCE:
        return Signal("payment_pattern", 2, PASS,
                      f"Rs {ext.amount:,.0f} within {int(AMOUNT_TOLERANCE*100)}% of avg",
                      "email_body vs vendor_master")
    return Signal("payment_pattern", 2, WARN,
                  f"Rs {ext.amount:,.0f} is {int(dev*100)}% off avg Rs {v.avg_payout_amount:,.0f}",
                  "email_body vs vendor_master")


# ── Policy ────────────────────────────────────────────────────────────

def decide(ext: ExtractionResult,
           fav: FAVResult,
           vendor: VendorRecord,
           other_vendor_accounts: Optional[Dict[str, str]] = None,
           near_duplicate: bool = False,
           split_below: bool = False,
           destination_account_number: Optional[str] = None) -> Decision:

    # Rule 1 — extraction failed
    if not ext.ok:
        return Decision(
            outcome=STEP_UP,
            rule_fired="R1_extraction_failed",
            reason=f"Extraction failed ({ext.failure_reason}). Held — "
                   f"inconclusive is not the same as clean.",
            triggered_by=["extraction_failure"],
            needs_callback=True,
        )

    dest, dest_src = resolve_destination(ext, destination_account_number)

    # Continuity runs on EVERY path, including the "nothing is changing" one —
    # verifying that claim is precisely what it is for.
    continuity = check_account_continuity(ext, vendor, other_vendor_accounts,
                                          destination_account_number)

    # Rule 2 — the request claims no change is being made. Check that claim
    # against the real destination rather than accepting it. This used to return
    # ALLOW on the intent label alone, which made one LLM output sufficient to
    # release a payout, and made any injection reaching PAYMENT_FOLLOWUP a total
    # bypass of identity validation.
    if ext.intent == INTENT_FOLLOWUP:
        if continuity.result == FAIL:
            return Decision(
                outcome=BLOCK,
                rule_fired="R2c_followup_destination_conflict",
                reason="Request asks for no destination change, but the payout is "
                       "headed to an account on file for a different vendor: "
                       + continuity.detail,
                triggered_by=["account_continuity"],
                tier1=[continuity],
                checked_destination=dest, destination_source=dest_src,
            )
        if continuity.result == WARN:
            return Decision(
                outcome=STEP_UP,
                rule_fired="R2b_followup_unverified_destination",
                reason="Request asks for no destination change, yet the destination "
                       "is not a known account for this vendor — the two statements "
                       "contradict each other. " + continuity.detail,
                triggered_by=["account_continuity"],
                tier1=[continuity],
                needs_callback=True,
                checked_destination=dest, destination_source=dest_src,
            )
        return Decision(
            outcome=ALLOW,
            rule_fired="R2a_no_change_confirmed",
            reason="Semantic layer read this as a payment follow-up, and the payout "
                   "destination is confirmed unchanged against the vendor master. "
                   + continuity.detail,
            triggered_by=[],
            tier1=[continuity],
            payout_allowed=True,
            checked_destination=dest, destination_source=dest_src,
        )

    # Run all checks
    t1 = [
        check_name_match(fav),
        check_account_status(fav),
        check_gstin(ext, vendor),
        continuity,
    ]
    t2 = [
        check_domain(ext, vendor),
        check_urgency(ext),
        check_channel_manipulation(ext),
        check_payment_pattern(ext, vendor, near_duplicate, split_below),
    ]

    t1_fail = [s for s in t1 if s.result == FAIL]
    t1_warn = [s for s in t1 if s.result == WARN]
    t2_warn = [s for s in t2 if s.result == WARN]

    new_account = any(s.name == "account_continuity" and s.result == WARN for s in t1)

    # Rule 3 — hard identity conflict
    if t1_fail:
        return Decision(
            outcome=BLOCK,
            rule_fired="R3_identity_conflict",
            reason="Identity conflict against the vendor master: "
                   + "; ".join(f"{s.name} — {s.detail}" for s in t1_fail),
            triggered_by=[s.name for s in t1_fail],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
        )

    # Rule 4 — BEC pattern: sever the old destination, under pressure,
    # from an unverified channel
    if ext.action == ACTION_REPLACE and new_account and len(t2_warn) >= 2:
        return Decision(
            outcome=BLOCK,
            rule_fired="R4_bec_pattern",
            reason=("Request would REPLACE the existing payout destination with a "
                    "previously unseen account, alongside " + str(len(t2_warn)) +
                    " contextual risk signals (" +
                    ", ".join(s.name for s in t2_warn) +
                    "). Every bank-level check may pass; change authorization is absent."),
            triggered_by=["account_continuity"] + [s.name for s in t2_warn],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
        )

    # Rule 5 — Tier 1 inconclusive
    if t1_warn:
        return Decision(
            outcome=STEP_UP,
            rule_fired="R5_tier1_inconclusive",
            reason="Identity evidence inconclusive: "
                   + "; ".join(f"{s.name} — {s.detail}" for s in t1_warn),
            triggered_by=[s.name for s in t1_warn],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
            needs_callback=True,
        )

    # Rule 6 — contextual risk
    if t2_warn:
        return Decision(
            outcome=STEP_UP,
            rule_fired="R6_contextual_risk",
            reason="Identity checks passed but contextual risk present: "
                   + ", ".join(s.name for s in t2_warn),
            triggered_by=[s.name for s in t2_warn],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
            needs_callback=True,
        )

    # Rule 7 — clean
    return Decision(
        outcome=ALLOW,
        rule_fired="R7_all_clear",
        reason="All identity checks passed against the vendor master; "
               "no contextual risk signals.",
        triggered_by=[],
        tier1=t1, tier2=t2,
        checked_destination=dest, destination_source=dest_src,
        payout_allowed=True,
    )
