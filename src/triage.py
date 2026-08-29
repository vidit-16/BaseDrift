"""
PayeeProof — triage.py

WHAT THIS IS FOR
================
Until v2 the system was hand-fed: POST /documents with a vendor_id already
attached. A real accounts-payable inbox is ~500 messages a day of invoices,
statements, chasers, delivery notes, internal mail, auto-replies and spam, and
nobody had done that routing. This file does it.

THE FUNNEL, CHEAPEST STAGE FIRST
================================
The volume argument is the whole design. At 20,000 payouts a day a merchant
sees roughly 500 inbound messages and perhaps 40 genuine change requests, of
which a handful are fraudulent. Spending an LLM call on all 500 is both
wasteful and slower than the useful work; spending one on the 40 is nothing.

  1  DEDUPE            message-id seen before -> drop.
  2  INGEST RULES      auto-replies, no-reply senders, empty or oversized
                       bodies. Header checks, no model.
  3  VENDOR RESOLUTION sender domain against the vendor master. NO MODEL.
                       Does ~90% of the filtering on its own.
  4  CLASSIFY          one model call, on what survives, to separate "this
                       message asks to change where money goes" from "this is
                       an invoice".

Stage 3 is the one that matters and the one with a trap in it.

THE TRAP IN STAGE 3, WRITTEN DOWN BEFORE IT WAS BUILT
=====================================================
The obvious rule is "is the sender's domain in the vendor master?" — and it
DROPS THE FRAUD. A typosquat is, by construction, not in the vendor master:
that is what makes it a typosquat. balaj1logistic.com would be filtered out as
an unknown sender, and the single most important class of message in this
system would never reach the decision engine at all.

Measured on the dev inbox: a domain allowlist discards 170 fraudulent change
requests, 64.6% of all the fraud in the mailbox, while improving every
operational number in sight.

So the stage is "in the master, OR a lookalike of something in it", reusing
decision_engine.is_lookalike_domain(). A message that resolves by lookalike is
flagged as such, and that flag survives into the decision as evidence.

AND THAT WAS STILL NOT ENOUGH — the second half, found by evaluating
--------------------------------------------------------------------
Domain matching, lookalikes included, still lost 119 genuine change requests on
the dev inbox: 51 legitimate rebrands (an acquired vendor writes from the new
parent's domain, which resembles nothing) and 68 fraud cases whose forged domain
was too far from the original to be a typosquat by edit distance.

The rebrands are the mirror image of the typosquat trap. Both are "a sender that
is not in the master", and both must be read.

So there is a third match: CONTENT. If the message quotes a GSTIN or a legal
name that IS in the master, it resolves to that vendor whatever the sender
domain says. This is what an AP clerk does with a mail from an unfamiliar
address. Measured: it recovered all 119, and pulled in 0 of the 1,474
unknown-sender noise messages, because ordinary business mail does not quote
another company's GST registration. Change requests surviving triage went from
66.1% to 100.0%.

A content match is a WEAKER claim than a domain match and is labelled as such —
anyone can type a GSTIN into an email. It is a reason to READ the message, never
evidence about who sent it.

WHICH VENDOR, AND WHY BEING WRONG HERE IS SURVIVABLE
====================================================
Triage's vendor is a ROUTING hint, not an identity finding. The decision engine
takes its vendor from the payout's own fund account at payout.pending, never
from the email. A message routed to the wrong vendor therefore fails to
correlate at the webhook and the payout is decided with
evidence_source="no_document_supplied" — which is a hold, not a release.

Ambiguity is reported rather than resolved by picking: candidates carries every
vendor that matched.

WHY MISROUTING HERE IS NOT A MONEY RISK
=======================================
Triage decides what gets READ. It does not decide anything about a payout.

The control point is the payout.pending webhook, which fires from RazorpayX
whether or not any email was ever seen. A change request that triage wrongly
drops does not become an approved payout — it becomes a payout arriving with no
document evidence, which webhook.py already reports as
evidence_source="no_document_supplied", and R2 then decides on the REAL
destination: a payout to a known account passes, one to an unseen account is
held, and one to another vendor's account still fires R2c. The failure mode of
this file is an unnecessary hold, never a release, and the mule check survives
the message never being read at all.

That is worth stating plainly because "we put an LLM in front of the inbox"
sounds like it should be the risky part, and the architecture is what makes it
not be.
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from decision_engine import VendorRecord, is_lookalike_domain

# ── Verdicts ──────────────────────────────────────────────────────────

DUPLICATE   = "DUPLICATE"       # seen this message-id already
DROPPED     = "DROPPED"         # machine noise: auto-reply, no-reply, empty
UNKNOWN     = "UNKNOWN_SENDER"  # no vendor, and not a lookalike of one
NOT_A_CHANGE = "NOT_A_CHANGE"   # a real vendor, but nothing about a destination
ROUTE       = "ROUTE"           # goes to the decision pipeline

# Stage names, so a verdict can be traced to the cheapest stage that produced it
S_DEDUPE, S_INGEST, S_VENDOR, S_CLASSIFY = "dedupe", "ingest", "vendor", "classify"

MAX_BODY_BYTES = 100_000

# Headers that mean "a machine sent this". RFC 3834 defines Auto-Submitted;
# the rest are what mail systems actually emit.
AUTO_HEADERS = {
    "auto-submitted": lambda v: v.strip().lower() != "no",
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
    "precedence": lambda v: v.strip().lower() in ("bulk", "auto_reply", "junk", "list"),
    "list-id": lambda v: True,
}

NOREPLY_LOCALS = ("no-reply", "noreply", "do-not-reply", "donotreply",
                  "mailer-daemon", "postmaster", "bounce", "bounces")


@dataclass
class Message:
    """One inbound message, as an MCP inbox tool would hand it over."""
    message_id:  str
    from_addr:   str
    subject:     str
    body:        str
    thread_id:   str = ""
    to_addr:     str = "accounts@clientcorp.in"
    received_at: float = 0.0
    headers:     Dict[str, str] = field(default_factory=dict)
    in_reply_to: str = ""

    @property
    def domain(self) -> str:
        return self.from_addr.split("@")[-1].strip().lower() if "@" in self.from_addr else ""

    @property
    def local_part(self) -> str:
        return self.from_addr.split("@")[0].strip().lower() if "@" in self.from_addr else ""

    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


@dataclass
class TriageResult:
    message_id:    str
    verdict:       str
    stage:         str
    reason:        str
    vendor_id:     Optional[str] = None
    # How the sender resolved. "lookalike" is EVIDENCE and travels onward — a
    # message that only resolved because it resembles a known vendor is exactly
    # the message this system exists for.
    match:         str = ""          # exact | lookalike | content | none
    matched_domain: str = ""
    # Every vendor the sender could be. More than one is a fact to report, not
    # a tie to break silently — and the payout, not this file, settles identity.
    candidates:    List[str] = field(default_factory=list)
    model_used:    Optional[str] = None

    @property
    def routed(self) -> bool:
        return self.verdict == ROUTE

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


# ── Stage 2: ingest rules ─────────────────────────────────────────────

def is_machine_mail(msg: Message) -> Tuple[bool, str]:
    lowered = {k.strip().lower(): v for k, v in (msg.headers or {}).items()}
    for header, test in AUTO_HEADERS.items():
        if header in lowered and test(str(lowered[header])):
            return True, f"{header}: {lowered[header]}"
    local = msg.local_part
    for pattern in NOREPLY_LOCALS:
        if pattern in local:
            return True, f"sender local-part {local!r} is a no-reply address"
    return False, ""


# ── Stage 3: vendor resolution, no model ──────────────────────────────

_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z]\d\b")


def resolve_vendor(msg: "Message",
                   vendors: Dict[str, VendorRecord]):
    """
    (vendor_id, match_kind, matched_domain, candidates).

    Three matches, strongest first. Each is weaker than the one before, and the
    kind travels onward so nothing downstream has to guess how the vendor was
    found.

      exact      the sender domain is in the vendor master
      lookalike  the sender domain is built to be mistaken for one that is.
                 NOT an enhancement: without it every typosquat is filtered out
                 as an unknown sender, and the fraud class this system exists
                 for never reaches the decision engine.
      content    the body quotes a GSTIN or legal name that is in the master.
                 Recovers legitimate rebrands — an acquired vendor writing from
                 the parent's domain — and forged domains too far from the
                 original to register as typosquats. Weakest of the three:
                 anyone can type a GSTIN. It is a reason to READ the message,
                 never evidence about who sent it.

    Ties are reported, not broken. `candidates` carries every vendor that
    matched, ordered deterministically, and vendor_id is simply the first.
    """
    domain = msg.domain if hasattr(msg, "domain") else ""
    items = sorted(vendors.items())

    exact = [vid for vid, v in items
             if v.known_domain and v.known_domain.strip().lower() == domain]
    if exact:
        return exact[0], "exact", vendors[exact[0]].known_domain, exact

    if domain:
        from decision_engine import _edit_distance
        near = [(_edit_distance(domain.split(".")[0], v.known_domain.split(".")[0]),
                 vid)
                for vid, v in items if is_lookalike_domain(domain, v.known_domain)]
        if near:
            near.sort()
            best = [vid for d, vid in near if d == near[0][0]]
            return best[0], "lookalike", vendors[best[0]].known_domain, best

    text = f"{getattr(msg, 'subject', '')}\n{getattr(msg, 'body', '')}"
    found_gstins = set(_GSTIN_RE.findall(text.upper()))
    lowered = text.lower()
    by_content = [vid for vid, v in items
                  if (v.gstin and v.gstin.upper() in found_gstins)
                  or (v.legal_name and v.legal_name.lower() in lowered)]
    if by_content:
        return by_content[0], "content", vendors[by_content[0]].known_domain, by_content

    return None, "none", "", []


# ── Stage 4: classification ───────────────────────────────────────────

# Deterministic pre-read. This is NOT the classifier — it is the cheap check
# that decides whether the classifier is worth calling, and it is deliberately
# generous: it errs toward calling the model, because a false NOT_A_CHANGE here
# is a message that never gets read.
_MONEY_SHAPE = re.compile(
    r"\b(?:account|ifsc|a/c|acct|beneficiar|bank|facility|settle|remit|"
    r"credit\s+to|transfer\s+to)\w*\b", re.I)

# The structural tell, which vocabulary misses: a long digit string next to an
# IFSC-shaped token. A message can name a destination without using any of the
# words above — "Details on your file are 416961125393 / SBIN0980865" did
# exactly that, and 73 genuine messages were dropped for it.
_DESTINATION_SHAPE = re.compile(
    r"\b\d{9,18}\b[^\n]{0,40}\b[A-Z]{4}0[A-Z0-9]{6}\b"
    r"|\b[A-Z]{4}0[A-Z0-9]{6}\b[^\n]{0,40}\b\d{9,18}\b")


def looks_like_it_touches_money(msg: Message) -> bool:
    """
    Deliberately generous. This decides whether the CLASSIFIER is worth calling,
    not whether the message matters, so a false positive costs one model call
    and a false negative costs a message nobody ever reads.
    """
    text = f"{msg.subject}\n{msg.body}"
    return bool(_MONEY_SHAPE.search(text) or _DESTINATION_SHAPE.search(text))


def classify(msg: Message, classifier=None) -> Tuple[bool, str, Optional[str]]:
    """
    (is_a_change_request, reason, model_used).

    classifier is injected so this file is testable without an API key and so
    the model can be swapped. When none is supplied the deterministic pre-read
    decides alone, which is weaker and is reported as such rather than being
    presented as a model result.

    A classifier that raises, times out, or returns nonsense routes the message
    ONWARD rather than dropping it. Inaction is the safe state here too: the
    cost of routing a chaser email is one extraction; the cost of dropping a
    change request is that nobody reads it.
    """
    cheap = looks_like_it_touches_money(msg)

    if classifier is None:
        return (cheap,
                ("mentions an account, a bank or a settlement destination"
                 if cheap else
                 "nothing in the subject or body refers to a payment destination "
                 "(deterministic pre-read only — no classifier supplied)"),
                None)

    if not cheap:
        return (False,
                "deterministic pre-read found no reference to a payment "
                "destination; not worth a model call",
                None)

    try:
        verdict = classifier(msg)
    except Exception as e:                                  # noqa: BLE001
        return (True,
                f"classifier failed ({type(e).__name__}); routed anyway — a "
                f"message nobody reads is the worse failure",
                None)

    if not isinstance(verdict, dict) or "is_change_request" not in verdict:
        return (True,
                "classifier returned an unusable response; routed anyway",
                verdict.get("model") if isinstance(verdict, dict) else None)

    return (bool(verdict["is_change_request"]),
            str(verdict.get("reason", "classifier"))[:300],
            verdict.get("model"))


# ── The funnel ────────────────────────────────────────────────────────

def triage(msg: Message,
           vendors: Dict[str, VendorRecord],
           seen_message_ids=None,
           classifier=None) -> TriageResult:
    """One message through the funnel. Never raises."""
    seen = seen_message_ids if seen_message_ids is not None else set()

    def result(verdict, stage, reason, **kw):
        return TriageResult(message_id=msg.message_id, verdict=verdict,
                            stage=stage, reason=reason, **kw)

    # 1 — dedupe. Same property the webhook needs for redelivered events: a
    # mailbox polled twice, or a message delivered to two folders, must not
    # produce two investigations.
    if msg.message_id in seen:
        return result(DUPLICATE, S_DEDUPE,
                      f"message-id {msg.message_id} already triaged")
    seen.add(msg.message_id)

    # 2 — ingest rules
    machine, why = is_machine_mail(msg)
    if machine:
        return result(DROPPED, S_INGEST, f"machine-generated mail — {why}")
    if not msg.body or not msg.body.strip():
        return result(DROPPED, S_INGEST, "empty body")
    if len(msg.body.encode("utf-8")) > MAX_BODY_BYTES:
        return result(DROPPED, S_INGEST,
                      f"body exceeds {MAX_BODY_BYTES} bytes; not read")

    # 3 — vendor resolution, no model
    vid, match, matched, candidates = resolve_vendor(msg, vendors)
    if vid is None:
        return result(UNKNOWN, S_VENDOR,
                      f"sender domain {msg.domain!r} is not in the vendor "
                      f"master, does not resemble anything in it, and the body "
                      f"quotes no identifier that is",
                      match="none")

    # 4 — classification
    is_change, reason, model = classify(msg, classifier)
    verdict = ROUTE if is_change else NOT_A_CHANGE
    return result(verdict, S_CLASSIFY, reason,
                  vendor_id=vid, match=match, matched_domain=matched,
                  candidates=candidates, model_used=model)


def triage_batch(messages: List[Message],
                 vendors: Dict[str, VendorRecord],
                 classifier=None) -> List[TriageResult]:
    seen: set = set()
    return [triage(m, vendors, seen, classifier) for m in messages]


def funnel_summary(results: List[TriageResult]) -> Dict[str, int]:
    """Counts per verdict, for the operational claim about volume."""
    out = {k: 0 for k in (DUPLICATE, DROPPED, UNKNOWN, NOT_A_CHANGE, ROUTE)}
    for r in results:
        out[r.verdict] = out.get(r.verdict, 0) + 1
    return out
