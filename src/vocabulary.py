"""
PayeeProof — operator vocabulary.

Every internal identifier, translated into what an accounts-payable person would
actually say. `R5_tier1_inconclusive` is a variable name; "Identity checks
incomplete" is the same fact in a form somebody can act on.

WHY THIS FILE, AND NOT JUST NICER STRINGS AT THE CALL SITE
==========================================================
Two audiences read the same decision and they need different things.

An OPERATOR needs to know what to do next. They should never have to learn that
R2c is the mule-account rule, or that INCONCLUSIVE and FAIL are different, or
that `razorpay_fav` is a bank lookup rather than an internal table.

An AUDITOR needs the exact rule that fired, because the rule table in
decision_engine.py is the authoritative policy and a paraphrase is not. So the
code is never thrown away — the dashboard shows the sentence and keeps the
identifier beside it in small type. Replacing one with the other would trade a
readable screen for an unauditable one.

Anything missing from these maps falls back to the raw code with underscores
turned to spaces, so a new rule shows up looking slightly rough rather than
crashing or, worse, silently displaying nothing.
"""

# ── What the engine decided ──────────────────────────────────────────

OUTCOME = {
    "ALLOW":          "Released",
    "STEP_UP_VERIFY": "On hold",
    "BLOCK":          "Rejected",
    "HELD":           "On hold",
    "DUPLICATE":      "Already processed",
}

# One line an operator can act on, per rule. The wording says what happened and
# implies the next step, rather than naming the mechanism.
RULE = {
    "R1_extraction_failed":
        "Could not read the request",
    "R2a_no_change_confirmed":
        "Routine payment — nothing was being changed",
    "R2b_followup_unverified_destination":
        "Money going somewhere new with nothing authorising it",
    "R2c_followup_destination_conflict":
        "Destination belongs to a different supplier",
    "R3_identity_conflict":
        "Details do not match this supplier's records",
    "R4_bec_pattern":
        "Impersonation evidence, and a new destination account",
    "R5_tier1_inconclusive":
        "Identity checks could not be completed",
    "R6_contextual_risk":
        "Circumstances around the request need checking",
    "R7_all_clear":
        "Every check passed",
    "R4_suppressed":
        "Below the review threshold",
}

# The one-line "so what" for a held payout, in the operator's terms.
RULE_NEXT_STEP = {
    "R1_extraction_failed":
        "Read the message yourself before deciding — the system could not.",
    "R2b_followup_unverified_destination":
        "The message says nothing is changing, but the money is going to an "
        "account we have not paid before. Ask the supplier which is true.",
    "R2c_followup_destination_conflict":
        "This account is already on file for another supplier. One account "
        "collecting from several suppliers is how one attacker harvests many.",
    "R3_identity_conflict":
        "Something the bank or our records say directly contradicts the "
        "request. Do not release on a phone call alone.",
    "R4_bec_pattern":
        "The sender is trying to be mistaken for this supplier and wants the "
        "destination replaced. Treat as fraud until proven otherwise.",
    "R5_tier1_inconclusive":
        "Not evidence of fraud — evidence we could not confirm identity. "
        "Verify through a channel the requester does not control.",
    "R6_contextual_risk":
        "Nothing is wrong with the identity, but the circumstances are "
        "unusual enough to confirm.",
}

# ── Signals ──────────────────────────────────────────────────────────

SIGNAL = {
    "name_match":            "Account holder's name",
    "account_status":        "Account can receive money",
    "gstin":                 "GST registration",
    "account_continuity":    "Destination account",
    "sender_domain":         "Sender's email domain",
    "urgency":               "Pressure to act quickly",
    "channel_manipulation":  "Asked to be contacted differently",
    "payment_pattern":       "Amount against this supplier's usual",
    "inbox_first_contact":   "First email ever from this sender",
    "inbox_sender_unrecognised": "Sender address not recognised",
    "inbox_thread_shallow":  "A reply with no conversation behind it",
}

RESULT = {
    "PASS":         "OK",
    "WARN":         "Concern",
    "INCONCLUSIVE": "Unverified",
    "FAIL":         "Mismatch",
}

# Where a finding came from. An operator deciding how much weight to give it
# needs to know whether the bank said it or the email claimed it.
SOURCE = {
    "razorpay_fav":                      "Bank account verification",
    "vendor_master":                     "Our supplier records",
    "vendor_master_crosscheck":          "Checked across all suppliers",
    "razorpay_payout":                   "The payout itself",
    "razorpay_fund_account":             "The payout itself",
    "razorpay_payout vs vendor_master":  "Payout, against our records",
    "email_body vs vendor_master":       "The email, against our records",
    "email_header":                      "The email header",
    "semantic_layer":                    "Read from the message",
    "mcp_inbox":                         "Mailbox history",
    "unresolved":                        "Nothing to check against",
    "authoritative_destination_malformed":
                                         "The payout's destination was unusable",
    "no_document_supplied":              "No change request on file",
    "llm_extraction":                    "Read from the message",
}

# ── How an account got onto the file ─────────────────────────────────
#
# The difference between these is the difference between a trust store and a
# list of numbers somebody emailed in.

ADDED_VIA = {
    "onboarding":    "At onboarding",
    "portal":        "Added through the supplier portal",
    "email_request": "Added because of an email request",
    "phone_request": "Added after a phone call",
}

VERIFIED_BY = {
    "onboarding_kyc": "Verified at onboarding (KYC)",
    "penny_drop":     "Verified by a rupee from the account",
    "callback":       "Verified by a callback",
    "unverified":     "Never verified outside email",
    "":               "Never verified outside email",
}


# ── Triage ───────────────────────────────────────────────────────────

VERDICT = {
    "ROUTE":          "Needs review",
    "DROPPED":        "Filtered out",
    "UNKNOWN_SENDER": "Sender not recognised",
    "NOT_A_CHANGE":   "Not about payment details",
    "DUPLICATE":      "Already seen",
}

MATCH = {
    "exact":     ("Known sender",
                  "the email domain is in our supplier records"),
    "lookalike": ("Lookalike domain",
                  "built to be mistaken for this supplier's real domain"),
    "content":   ("Unrecognised sender",
                  "matched only by an identifier quoted in the message, "
                  "which anyone can type"),
    "none":      ("No supplier matched", ""),
}

# ── Verification ─────────────────────────────────────────────────────

VERIFICATION = {
    "CONFIRMED":             "Supplier confirmed it by phone",
    "CONTROL_PROVEN":        "Supplier proved they control the old account",
    "CONTESTED":             "Answered the phone, could not send the rupee",
    "UNREACHABLE":           "Could not reach the supplier",
    "REJECTED":              "Supplier says they did not send this",
    "CHANNEL_2_UNAVAILABLE": "No account exists that could prove control",
}


def _fallback(code):
    return str(code).replace("_", " ").strip().capitalize() if code else "—"


def outcome(code):
    return OUTCOME.get(code, _fallback(code))


def rule(code):
    return RULE.get(code, _fallback(code))


def next_step(code):
    return RULE_NEXT_STEP.get(code, "")


def signal(code):
    return SIGNAL.get(code, _fallback(code))


def result(code):
    return RESULT.get(code, _fallback(code))


def source(code):
    return SOURCE.get(code, _fallback(code))


def verdict(code):
    return VERDICT.get(code, _fallback(code))


def verification(code):
    return VERIFICATION.get(code, _fallback(code))


def match(code):
    return MATCH.get(code, (_fallback(code), ""))


def added_via(code):
    return ADDED_VIA.get(code, _fallback(code))


def verified_by(code):
    return VERIFIED_BY.get(code, _fallback(code))
