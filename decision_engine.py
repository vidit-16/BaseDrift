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

TIER 1 — identity binding (against vendor master, never against request)

  name_match_score (from FAV):
    >= 85       PASS
    60-84       WARN    graduated: "Pvt Ltd" vs "Private Limited" etc.
    < 60        FAIL

  gstin:
    matches, not hedged     PASS
    matches, hedged         WARN    weaker claim
    absent                  WARN
    mismatch                FAIL

  account_continuity:
    same as a known account          PASS
    new account                      WARN    legitimate changes exist
    account seen on another vendor   FAIL    cross-contact reuse

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

  2. intent == PAYMENT_FOLLOWUP                 ALLOW
     no destination change requested; nothing to authorize

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

    def to_dict(self):
        return {
            "outcome": self.outcome,
            "rule_fired": self.rule_fired,
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "payout_allowed": self.payout_allowed,
            "needs_callback": self.needs_callback,
            "tier1": [vars(s) for s in self.tier1],
            "tier2": [vars(s) for s in self.tier2],
        }


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


def check_gstin(ext: ExtractionResult, v: VendorRecord) -> Signal:
    proposed = ext.proposed_gstin
    if proposed is None:
        return Signal("gstin", 1, WARN, "no GSTIN in request", "email_body")
    if proposed.upper() != v.gstin.upper():
        return Signal("gstin", 1, FAIL,
                      f"mismatch: {proposed} vs master {v.gstin}",
                      "email_body vs vendor_master")
    if any(f.lower() in ("gstin", "proposed_gstin") for f in ext.hedged_fields):
        return Signal("gstin", 1, WARN,
                      f"{proposed} matches but the claim was hedged",
                      "email_body vs vendor_master")
    return Signal("gstin", 1, PASS, f"{proposed} matches vendor master",
                  "email_body vs vendor_master")


def check_account_continuity(ext: ExtractionResult, v: VendorRecord,
                              other_vendor_accounts: Optional[Dict[str, str]] = None) -> Signal:
    """
    other_vendor_accounts: {account_number: vendor_id} across the whole
    vendor master. RazorpayX allows the same account under multiple
    contacts, which is legitimate for corporate groups but is also
    exactly how one attacker redirects several vendors to one destination.
    """
    proposed = ext.proposed_account_number
    if proposed is None:
        return Signal("account_continuity", 1, WARN,
                      "no account number in request", "email_body")

    if other_vendor_accounts:
        owner = other_vendor_accounts.get(proposed)
        if owner and owner != v.vendor_id:
            return Signal("account_continuity", 1, FAIL,
                          f"account {proposed} is already on file for a different "
                          f"vendor ({owner}) — cross-contact reuse",
                          "vendor_master_crosscheck")

    if proposed in v.all_known_accounts():
        return Signal("account_continuity", 1, PASS,
                      f"{proposed} is a known account for this vendor",
                      "vendor_master")

    return Signal("account_continuity", 1, WARN,
                  f"new account {proposed} — known: {v.all_known_accounts()}",
                  "email_body vs vendor_master")


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
           split_below: bool = False) -> Decision:

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

    # Rule 2 — no change requested
    if ext.intent == INTENT_FOLLOWUP:
        return Decision(
            outcome=ALLOW,
            rule_fired="R2_no_change_requested",
            reason="Semantic layer read this as a payment follow-up, not a "
                   "destination change. Nothing to authorize.",
            triggered_by=[],
            payout_allowed=True,
        )

    # Run all checks
    t1 = [
        check_name_match(fav),
        check_gstin(ext, vendor),
        check_account_continuity(ext, vendor, other_vendor_accounts),
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
        payout_allowed=True,
    )
