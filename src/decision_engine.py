"""
BaseDrift — decision_engine.py

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
    inactive    INCONCLUSIVE  the bank says it cannot receive right now,
                              which is a reason to ask, not to refuse
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

  sender_domain:          match PASS
                          lookalike of the known domain  WARN + DECEPTION
                          some other domain              WARN
                          not found                      INCONCLUSIVE
  A typosquat and a rebrand are both "not the known domain" and are not
  remotely the same evidence. balaj1logistic.com is built to be mistaken for
  balajilogistic.com; balajilogisticsgroup.com is what an acquisition looks
  like. Only the first is evidence of intent to deceive.
  urgency:                absent PASS / present WARN
  channel_manipulation:   absent PASS / present WARN
  payment_pattern:        within 15% of avg and no flags PASS
                          deviation or a split/near-duplicate flag  WARN
                          no amount stated  INCONCLUSIVE

SIGNAL STATES — four, not three
  PASS          checked, clean
  WARN          checked, adverse evidence
  INCONCLUSIVE  could not check
  FAIL          checked, direct conflict with the vendor master

  WARN used to carry both "looks wrong" and "could not check". Both must hold a
  payout, but only adverse evidence may contribute to a BLOCK. Under the old
  scheme a message that simply omitted an amount gained a risk signal, which
  pushed cases toward rejection for having less information rather than worse
  information. R4 counts WARN only; R5 and R6 hold on either.

═══════════════════════════════════════════════════════════════════════
DECISION POLICY — first match wins
═══════════════════════════════════════════════════════════════════════

NOTHING REJECTS UNATTENDED. The harshest automatic outcome this engine can
reach is a HOLD. Rules that once rejected now set recommended_action="reject"
and wait for a human, exactly as fund-account deactivation already did.
Rejecting a payout is recoverable only in principle — it stops a real vendor
being paid and can break payment terms. A hold costs a phone call. And a
rejection prevents no fraud a hold does not: in both cases the money stays put.

  1. extraction failed                          STEP_UP
     "we couldn't check" != "we found fraud"

  2. intent == PAYMENT_FOLLOWUP — the request claims nothing is changing.
     VERIFY that claim against the resolved destination; do not take it.
     2a. destination is a known account          ALLOW
         claim corroborated; there is genuinely nothing to authorize
     2b. destination unknown / unresolvable      STEP_UP
         the layer says "no change" while the money goes somewhere new,
         or we cannot tell where it goes — a contradiction, not a clearance
     2c. destination belongs to another vendor   HOLD + recommend reject

     Rule 2 used to return ALLOW on the intent label alone, before any
     identity check ran. That made a single LLM output sufficient to release
     a payout, and made a prompt injection that reached PAYMENT_FOLLOWUP a
     complete bypass. The semantic layer is evidence; it is not authority.
     Only continuity runs here — a follow-up requests no change, so the one
     thing that must hold is that the destination really is unchanged.
     Running the full battery would step up every routine follow-up.

  3. any Tier 1 FAIL                            HOLD + recommend reject

  4. REPLACE + new account + a DECEPTION signal
                            + >=1 Tier 2 WARN THAT IS NOT THE DECEPTION SIGNAL
                                                HOLD + recommend reject
     (WARN only — INCONCLUSIVE signals never contribute to a rejection)

     A rejection requires evidence that someone is trying to be mistaken for
     the vendor. Contextual signals corroborate that evidence; they cannot
     substitute for it.

     The corroboration must be INDEPENDENT, and for a long time it was not.
     The condition read "deception and >=1 Tier 2 WARN", but the only signal
     that sets deception is sender_domain, which is itself a Tier 2 WARN — so
     the second clause was satisfied by the first and never constrained
     anything. Deleting Tier 2 from the engine entirely gave numbers identical
     to deleting R6 alone, which is how it surfaced.

     The audit record was the real casualty: the reason string read
     "corroborated by 2 contextual risk signal(s) (sender_domain, urgency)",
     counting the impersonation as its own corroboration, and on 4 of 62 dev
     firings sender_domain was the only Tier 2 warning at all. An operator
     reads that sentence before recommending a payment be refused.

     This rule previously fired on REPLACE + new account + any 2 Tier 2 WARNs.
     Both of those inputs are true of an ordinary, legitimate bank change: a
     new account is what changing banks MEANS, and urgency plus an unfamiliar
     domain is what an acquired company's finance team looks like. Measured on
     the dev set, that version rejected 15.8% of all legitimate traffic — 49 of
     60 rebrand cases — and no threshold fixed it: tightening to 4 warns cut
     false blocks to 0.6% but dropped recall to 85.4%, no better than holding
     every payout and phoning the vendor.

     It also contradicted this table's own stated invariant that Tier 2 is
     "never decisive alone". A new account is the only Tier 1 input, and it
     carries no evidence of fraud whatsoever.

     Note what follows: for a compromised MAILBOX — real domain, correct GSTIN,
     genuine name match — there is no deception signal and no identity conflict.
     Such a request is indistinguishable from a legitimate change on evidence
     alone, and it correctly falls through to STEP_UP. The callback, not the
     rule table, is what resolves it. That is the honest boundary of what
     evidence can do here.
     the BEC pattern: cut off the old destination, under pressure,
     from an unverified channel

  5. any Tier 1 WARN or INCONCLUSIVE            STEP_UP

  6. any Tier 2 WARN or INCONCLUSIVE            STEP_UP

     Tier 2 also carries INBOX evidence when the caller supplies it (V2.3):
     first contact, a reply with no conversation behind it, a sender who keeps
     moving the destination, a vendor identified only from text in the body.
     Every one is WARN or INCONCLUSIVE and never PASS, enforced at the point
     they are added, because a mailbox owner can author their own history.

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

# Four states, not three. WARN was carrying two incompatible meanings:
# "I checked this and it looks wrong" and "I could not check this". Both must
# hold a payout, but only the first is EVIDENCE OF FRAUD and may contribute to
# a BLOCK. Conflating them let missing data push a case toward rejection.
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"

# Anything that is not a clean pass blocks an automatic ALLOW.
NOT_CLEAN = (WARN, INCONCLUSIVE, FAIL)


# ── Data ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    name:   str
    tier:   int
    result: str      # PASS | WARN | INCONCLUSIVE | FAIL
    detail: str
    source: str      # provenance: where the evidence came from
    # True when this signal is evidence that someone is trying to be MISTAKEN
    # for the vendor, as distinct from evidence that something is merely
    # unusual. Only deception may drive a rejection (R4); everything else
    # corroborates. Kept as a flag rather than a tier so future deception
    # signals can be added without reshuffling the tiers.
    deception: bool = False


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
class AccountRecord:
    """
    One account on file, with the provenance that decides whether it can be
    trusted for anything. The account number alone is not enough: the vendor
    master is the root of trust for every check here, so an account that entered
    it unverified is worthless as an anchor — the destination would be checked
    against a record an attacker could have written.
    """
    account_number:       str
    ifsc:                 str = ""
    status:               str = "active"
    added_on:             str = ""     # ISO date
    added_via:            str = ""     # onboarding | portal | email_request | phone_request
    verified_by:          str = ""     # onboarding_kyc | penny_drop | callback | unverified
    verified_on:          str = ""
    settled_payout_count: int = 0
    last_settled_on:      str = ""
    is_primary:           bool = False


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
    # A DECLARED corporate group, never inferred from a shared account — a
    # shared account is the thing being judged. Blank means no group, and two
    # blanks are NOT a match; see same_group().
    group_id:             str = ""
    # Every account on file, primary included. RazorpayX permits several fund
    # accounts per contact, and until v2 this system pretended otherwise: the
    # field existed, nothing populated it, and a vendor with a genuine second
    # account was held on every payout to it, permanently.
    accounts:             List["AccountRecord"] = field(default_factory=list)

    def all_known_accounts(self) -> List[str]:
        known = [self.known_account_number] if self.known_account_number else []
        for a in self.accounts:
            if a.account_number not in known:
                known.append(a.account_number)
        return known

    def account(self, number: str) -> Optional["AccountRecord"]:
        for a in self.accounts:
            if a.account_number == number:
                return a
        return None


def same_group(a: VendorRecord, b: VendorRecord) -> bool:
    """
    Two vendors in one declared corporate group.

    The `and a.group_id` is the whole function. Without it two blanks compare
    equal, every ungrouped pair in the master reads as a group, and the
    cross-contact reuse check — the one that catches the mule pattern — is off
    for the ~60% of vendors that belong to no group at all.
    """
    return bool(a.group_id) and a.group_id == b.group_id


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
    # What the engine would do if a human agreed. Rejection is no longer
    # automatic: a BLOCK stops no fraud that a HOLD does not also stop — the
    # money does not move either way — so it bought operational convenience and
    # paid for it with the only customer-facing failure this system has.
    recommended_action: Optional[str] = None
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
            "recommended_action": self.recommended_action,
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
    if destination_account_number is not None:
        dest = str(destination_account_number).strip()
        if dest:
            return dest, "razorpay_payout"
        # Supplied, but blank or whitespace. Do NOT fall back to the request's
        # own claim. The caller passing this parameter asserted it had
        # authoritative data; substituting a self-reported account number when
        # that data turns out to be missing is precisely the swap this control
        # exists to prevent, and it previously produced an outright ALLOW.
        return None, "authoritative_destination_malformed"
    if ext.proposed_account_number:
        return ext.proposed_account_number, "email_claim_only"
    return None, "unresolved"


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein. Small and dependency-free — requirements.txt is one line."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# A typosquat is usually one or two substituted characters — the whole point is
# that it survives a glance. Anything further away is a different name, which
# may be a rebrand, a subsidiary, or an unrelated party, but is not an attempt
# to be misread as this vendor.
LOOKALIKE_MAX_EDITS = 2


def is_lookalike_domain(sender: str, known: str) -> bool:
    """
    True when `sender` appears built to be mistaken for `known`.

    Deliberately narrow. A domain that is merely DIFFERENT is not evidence of
    deception — acquisitions and group consolidations produce those routinely,
    and treating them as fraud is what rejected legitimate vendors.
    """
    if not sender or not known:
        return False
    a, b = sender.strip().lower(), known.strip().lower()
    if a == b:
        return False
    # Compare the registrable label, so a TLD change alone is not a typosquat.
    sa, sb = a.split(".")[0], b.split(".")[0]
    if sa == sb:
        return False
    if abs(len(sa) - len(sb)) > LOOKALIKE_MAX_EDITS:
        return False
    return _edit_distance(sa, sb) <= LOOKALIKE_MAX_EDITS


def _phrases(items, limit: int = 2) -> str:
    """
    Join model-supplied phrases for a signal detail string.

    Coerces each element rather than assuming it is a string. validate() now
    rejects non-string elements upstream, but the engine is also fed directly by
    the evaluators and tests, and a rule engine that can be crashed by its own
    input is a denial-of-service on the payout queue.
    """
    return "; ".join(str(x) for x in (items or [])[:limit])


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
        return Signal("name_match", 1, INCONCLUSIVE,
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
        # INCONCLUSIVE, not FAIL. This was an overcorrection: the field went
        # from being read by nothing at all to being a hard identity conflict.
        # It caused ALL FIVE false rejections across both v1 splits, occurs on
        # ~2% of cases in each, and is UNCORRELATED with fraud.
        #
        # "The bank says this account cannot receive right now" is a reason to
        # ask the vendor, not to refuse them: a dormant, newly opened or
        # KYC-pending account looks exactly like this and is not fraud.
        return Signal("account_status", 1, INCONCLUSIVE,
                      "bank reports the account inactive — it cannot receive; "
                      "a reason to ask, not to refuse",
                      "razorpay_fav")
    return Signal("account_status", 1, INCONCLUSIVE,
                  f"account status {st or 'missing'!r} — FAV inconclusive, not clean",
                  "razorpay_fav")


def check_gstin(ext: ExtractionResult, v: VendorRecord) -> Signal:
    proposed = ext.proposed_gstin
    if proposed is None:
        return Signal("gstin", 1, INCONCLUSIVE, "no GSTIN in request", "email_body")
    if proposed.upper() != v.gstin.upper():
        return Signal("gstin", 1, FAIL,
                      f"mismatch: {proposed} vs master {v.gstin}",
                      "email_body vs vendor_master")
    if _hedged(ext.hedged_fields, "gst"):
        return Signal("gstin", 1, INCONCLUSIVE,
                      f"{proposed} matches but the claim was hedged",
                      "email_body vs vendor_master")
    if ext.hedging_detected and not ext.hedged_fields:
        # Hedging was detected but the model did not say where. Treat the GSTIN
        # claim as hedged: the cost is a callback, and "we cannot tell" resolving
        # to WARN is the same fail-safe direction as every other rule here.
        return Signal("gstin", 1, INCONCLUSIVE,
                      f"{proposed} matches, but hedging was detected with no field "
                      f"named — treated as hedged",
                      "email_body vs vendor_master")
    return Signal("gstin", 1, PASS, f"{proposed} matches vendor master",
                  "email_body vs vendor_master")


def _unanchored(v: VendorRecord, dest: str, prov: str, src: str
                ) -> Optional[Signal]:
    """
    "On file" is not one thing, and treating it as one is how the trust store
    gets poisoned.

    AccountRecord's own docstring already says it: an account that entered the
    master unverified is worthless as an anchor, because the destination would
    be checked against a record an attacker could have written. That reasoning
    was applied to the account a penny drop is demanded FROM (V2.6) and never to
    the destination being paid TO — so an attacker who got an account onto the
    master with one accepted email request could later ask for the money and
    every identity check would pass, honestly, on a fact that was itself the
    fraud. Measured: 19 of 35 such cases released. See BUILD-LOG.md V2.G.

    The predicate is not "was this planted" — nothing can know that. It is
    "has this account ever been confirmed by anything outside an email, or ever
    actually carried a settlement". Neither is true of a planted account, both
    are true of an onboarding account, and at least one is true of every
    legitimate second account in this corpus.

    INCONCLUSIVE, not WARN: this is "could not confirm", not "looks wrong", and
    the project rule is that missing data never pushes a case toward rejection.
    It holds the payout and asks a human, which is the whole design.

    Returns None when there is nothing to object to, or when the account carries
    no provenance at all — a caller that built a VendorRecord without
    AccountRecords is not asserting the account is weak, it simply has not said.
    """
    a = v.account(dest)
    if a is None:
        return None
    if a.verified_by not in ("", "unverified"):
        return None
    if a.settled_payout_count > 0:
        return None
    return Signal("account_continuity", 1, INCONCLUSIVE,
                  f"{dest} ({prov}) is on file for this vendor but has never "
                  f"been verified by anything outside email "
                  f"(added_via {a.added_via or 'unrecorded'}, "
                  f"verified_by {a.verified_by or 'unrecorded'}) and has never "
                  f"settled a payout — being on file is not the same as being "
                  f"established",
                  f"{src} vs vendor_master")


def check_account_continuity(ext: ExtractionResult, v: VendorRecord,
                              other_vendor_accounts=None,
                              destination_account_number: Optional[str] = None,
                              vendors: Optional[Dict[str, VendorRecord]] = None
                              ) -> Signal:
    """
    Checks the account the money will actually reach — see resolve_destination().

    other_vendor_accounts: {account_number: set(vendor_ids)} across the whole
    vendor master. RazorpayX allows the same account under multiple contacts,
    which is legitimate for corporate groups and is also exactly how one
    attacker redirects several vendors to one destination. Those two are
    indistinguishable without an explicit group, which is why this used to
    reject the legitimate one.

    It was a Dict[str, str] built with `idx[acct] = vendor_id` in a loop, so a
    second vendor SILENTLY OVERWROTE the first and a payout to a shared account
    fired the cross-contact FAIL for whichever vendor lost the race. The dict
    was the bug; the rule reading it was correct all along.

    A plain {account: vendor_id} mapping is still accepted so callers that have
    not migrated keep working, and is read as a one-element set.
    """
    dest, src = resolve_destination(ext, destination_account_number)

    if dest is None:
        if src == "authoritative_destination_malformed":
            return Signal("account_continuity", 1, INCONCLUSIVE,
                          "the payout's destination was supplied but is blank or "
                          "malformed; refusing to substitute the account number "
                          "claimed in the request",
                          src)
        return Signal("account_continuity", 1, INCONCLUSIVE,
                      "no destination could be resolved — neither the payout's "
                      "fund account nor an account number in the request",
                      "unresolved")

    # Provenance is stated in every detail string: a reader must be able to tell
    # a verified destination from a self-reported one at a glance.
    prov = ("payout fund account" if src == "razorpay_payout"
            else "UNVERIFIED — from the request, no payout destination supplied")

    if other_vendor_accounts:
        owners = other_vendor_accounts.get(dest) or set()
        if isinstance(owners, str):
            owners = {owners}
        strangers = {o for o in owners if o != v.vendor_id}

        # A sibling in the same DECLARED group is not a stranger. One treasury
        # facility across a group's companies is ordinary, and rejecting it was
        # a reproducible false rejection, not a theoretical one.
        siblings, outsiders = set(), set()
        for o in strangers:
            other = (vendors or {}).get(o)
            if other is not None and same_group(other, v):
                siblings.add(o)
            else:
                outsiders.add(o)

        if outsiders:
            return Signal("account_continuity", 1, FAIL,
                          f"account {dest} ({prov}) is already on file for a "
                          f"different vendor ({', '.join(sorted(outsiders))}) "
                          f"outside this vendor's declared group — "
                          f"cross-contact reuse",
                          "vendor_master_crosscheck")

        if siblings and dest in v.all_known_accounts():
            weak = _unanchored(v, dest, prov, src)
            return weak or Signal("account_continuity", 1, PASS,
                                  f"{dest} ({prov}) is a shared facility inside "
                                  f"declared group {v.group_id}, with "
                                  f"{', '.join(sorted(siblings))}",
                                  f"{src} vs vendor_master")

    if dest in v.all_known_accounts():
        weak = _unanchored(v, dest, prov, src)
        return weak or Signal("account_continuity", 1, PASS,
                              f"{dest} ({prov}) is a known account for this vendor",
                              f"{src} vs vendor_master")

    return Signal("account_continuity", 1, WARN,
                  f"new account {dest} ({prov}) — known: {v.all_known_accounts()}",
                  f"{src} vs vendor_master")


# ── Tier 2 ────────────────────────────────────────────────────────────

def check_domain(ext: ExtractionResult, v: VendorRecord) -> Signal:
    d = ext.sender_domain
    if d is None:
        return Signal("sender_domain", 2, INCONCLUSIVE, "sender domain not found",
                      "email_header")
    if d.lower() == v.known_domain.lower():
        return Signal("sender_domain", 2, PASS, f"{d} matches known domain",
                      "email_header vs vendor_master")
    if is_lookalike_domain(d, v.known_domain):
        return Signal("sender_domain", 2, WARN,
                      f"{d} is a lookalike of {v.known_domain} — built to be "
                      f"misread as this vendor",
                      "email_header vs vendor_master",
                      deception=True)
    return Signal("sender_domain", 2, WARN,
                  f"{d} != known {v.known_domain} (different domain, not a "
                  f"lookalike — could be a rebrand)",
                  "email_header vs vendor_master")


def check_urgency(ext: ExtractionResult) -> Signal:
    if not ext.urgency_detected:
        return Signal("urgency", 2, PASS, "no urgency language", "semantic_layer")
    phrases = _phrases(ext.urgency_phrases) or "detected"
    return Signal("urgency", 2, WARN, f"urgency: {phrases}", "semantic_layer")


def check_channel_manipulation(ext: ExtractionResult) -> Signal:
    if not ext.channel_manipulation_detected:
        return Signal("channel_manipulation", 2, PASS,
                      "no channel redirection", "semantic_layer")
    phrases = _phrases(ext.channel_manipulation_phrases) or "detected"
    return Signal("channel_manipulation", 2, WARN,
                  f"redirecting communication: {phrases}", "semantic_layer")


def check_payment_pattern(ext: ExtractionResult, v: VendorRecord,
                           near_duplicate=False, split_below=False) -> Signal:
    if ext.amount is None:
        return Signal("payment_pattern", 2, INCONCLUSIVE,
                      "no amount stated — cannot compare against the vendor baseline",
                      "email_body")

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
           destination_account_number: Optional[str] = None,
           # The whole master, so a shared account can be checked against a
           # DECLARED group. Optional: callers that do not pass it get the old
           # behaviour, where any second owner is a stranger.
           vendors: Optional[Dict[str, VendorRecord]] = None,
           # Signals derived from the merchant's mailbox. Rejected at the door
           # unless Tier 2 and non-PASS; see the check where they are appended.
           inbox_signals: Optional[List[Signal]] = None) -> Decision:

    # Inbox evidence (V2.3), validated BEFORE any rule runs.
    #
    # It sat at the point of use at first, which put it after R1 and R2 — so a
    # follow-up short-circuited to ALLOW without the check ever running, and
    # whether a smuggled signal was caught depended on which rule fired. A
    # guard that only some paths reach is not a guard.
    #
    # TIER 2 ONLY, never PASS: no amount of mailbox history can satisfy a rule
    # that requires a clean signal. A mailbox owner can send themselves
    # messages and author their own correspondence, so evidence an attacker can
    # write must never be able to say yes. See
    # inbox_signals.assert_cannot_release().
    for sig in (inbox_signals or []):
        if sig.tier != 2 or sig.result == PASS:
            raise ValueError(
                f"inbox signal {sig.name!r} is Tier {sig.tier}/{sig.result}; "
                f"inbox evidence is Tier 2 and may never PASS")

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
                                          destination_account_number, vendors)

    # Rule 2 — the request claims no change is being made. Check that claim
    # against the real destination rather than accepting it. This used to return
    # ALLOW on the intent label alone, which made one LLM output sufficient to
    # release a payout, and made any injection reaching PAYMENT_FOLLOWUP a total
    # bypass of identity validation.
    if ext.intent == INTENT_FOLLOWUP:
        if continuity.result == FAIL:
            return Decision(
                outcome=STEP_UP,
                recommended_action="reject",
                needs_callback=True,
                rule_fired="R2c_followup_destination_conflict",
                reason="Request asks for no destination change, but the payout is "
                       "headed to an account on file for a different vendor: "
                       + continuity.detail,
                triggered_by=["account_continuity"],
                tier1=[continuity],
                checked_destination=dest, destination_source=dest_src,
            )
        # WARN *or* INCONCLUSIVE. Only a clean PASS releases here — falling
        # through to ALLOW on "could not resolve the destination" would reopen
        # exactly the bypass this rule exists to close.
        if continuity.result in (WARN, INCONCLUSIVE):
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
        if continuity.result != PASS:      # unreachable today; stays fail-safe
            return Decision(
                outcome=STEP_UP,
                rule_fired="R2b_followup_unverified_destination",
                reason="Destination could not be positively confirmed: "
                       + continuity.detail,
                triggered_by=["account_continuity"],
                tier1=[continuity], needs_callback=True,
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
    t2.extend(inbox_signals or [])

    t1_fail = [s for s in t1 if s.result == FAIL]
    # "Not clean" holds the payout (R5/R6). Only WARN — actual adverse evidence —
    # counts toward R4's BLOCK. A case is never rejected for missing data.
    t1_unclean = [s for s in t1 if s.result in (WARN, INCONCLUSIVE)]
    t2_unclean = [s for s in t2 if s.result in (WARN, INCONCLUSIVE)]
    t2_warn = [s for s in t2 if s.result == WARN]

    # A previously unseen destination is adverse evidence, not missing data —
    # it stays a WARN and legitimately contributes to the BEC pattern.
    new_account = any(s.name == "account_continuity" and s.result == WARN for s in t1)

    # Rule 3 — hard identity conflict
    if t1_fail:
        return Decision(
            outcome=STEP_UP,
            recommended_action="reject",
            needs_callback=True,
            rule_fired="R3_identity_conflict",
            reason="Identity conflict against the vendor master: "
                   + "; ".join(f"{s.name} — {s.detail}" for s in t1_fail),
            triggered_by=[s.name for s in t1_fail],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
        )

    # Rule 4 — BEC pattern: sever the old destination, under pressure,
    # from an unverified channel
    deception = [sig for sig in t1 + t2 if sig.deception]

    # THE CORROBORATION HAS TO COME FROM SOMETHING ELSE.
    #
    # This used to read `deception and len(t2_warn) >= 1`, which never
    # constrained anything: the only signal that sets deception is
    # sender_domain, and sender_domain is itself a Tier 2 WARN — so the second
    # clause was satisfied by the first. Deleting Tier 2 from the engine
    # entirely produced identical numbers to deleting R6 alone, which is how it
    # was found.
    #
    # It was not merely redundant, it made the audit record untrue. The reason
    # string said "corroborated by 2 contextual risk signal(s) (sender_domain,
    # urgency)", counting the impersonation as its own corroboration, and on 4
    # of 62 dev firings sender_domain was the ONLY Tier 2 warning — the rule
    # claimed corroboration where none existed. An operator reads that sentence
    # before recommending somebody's payment be refused.
    #
    # Cost of requiring an independent signal, measured on both splits: R4 fires
    # 58 instead of 62 on dev and 24 instead of 26 on holdout. Those cases are
    # still HELD, by R5 or R6; what they lose is the rejection recommendation,
    # which is exactly the thing that should need corroborating.
    corroboration = [s for s in t2_warn if not s.deception]

    if (ext.action == ACTION_REPLACE and new_account
            and deception and corroboration):
        return Decision(
            outcome=STEP_UP,
            recommended_action="reject",
            needs_callback=True,
            rule_fired="R4_bec_pattern",
            reason=("Request would REPLACE the existing payout destination with a "
                    "previously unseen account, and carries evidence of deliberate "
                    "impersonation (" +
                    "; ".join(s.detail for s in deception) +
                    "), corroborated independently by " +
                    str(len(corroboration)) + " contextual risk signal(s) (" +
                    ", ".join(s.name for s in corroboration) +
                    "). Every bank-level check may pass; change authorization is absent."),
            triggered_by=(["account_continuity"]
                          + [s.name for s in deception]
                          + [s.name for s in corroboration]),
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
        )

    # Rule 5 — Tier 1 inconclusive
    if t1_unclean:
        return Decision(
            outcome=STEP_UP,
            rule_fired="R5_tier1_inconclusive",
            reason="Identity evidence inconclusive: "
                   + "; ".join(f"{s.name} — {s.detail}" for s in t1_unclean),
            triggered_by=[s.name for s in t1_unclean],
            tier1=t1, tier2=t2,
            checked_destination=dest, destination_source=dest_src,
            needs_callback=True,
        )

    # Rule 6 — contextual risk
    if t2_unclean:
        return Decision(
            outcome=STEP_UP,
            rule_fired="R6_contextual_risk",
            reason="Identity checks passed but contextual risk present: "
                   + ", ".join(s.name for s in t2_unclean),
            triggered_by=[s.name for s in t2_unclean],
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
