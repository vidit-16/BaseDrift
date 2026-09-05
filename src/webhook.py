"""
BaseDrift — payout.pending webhook handler.

This is the integration point the README's first paragraph describes: RazorpayX
fires `payout.pending` while the payout is frozen and no money has moved, and
BaseDrift decides before it thaws.

FAIL-SAFE BY CONSTRUCTION
=========================
The safe state is INACTION. A pending payout stays pending until something
explicitly approves it, so every failure mode here — bad signature, unknown
fund account, unknown vendor, missing document, crashed process, BaseDrift
being down entirely — leaves the payout held. There is no code path where an
error releases money. That is why the handler never "defaults to approve" on
anything, and why a 500 is an acceptable outcome: Razorpay retries, and the
payout is safe in the meantime.

THE CORRELATION PROBLEM, AND WHY IT IS NOT HAND-WAVED
=====================================================
A payout.pending event tells you money is about to move. It does NOT tell you
what document requested that destination — and BaseDrift's whole question is
about the provenance of a change request. Two sources are needed, and only one
arrives on the webhook.

The merchant's AP system supplies the other, via POST /documents. Correlation
then runs in this order:

  1. payout.notes.basedrift_document_id  — an explicit reference, unambiguous
  2. the most recent document for that vendor inside CORRELATION_WINDOW_DAYS
  3. nothing found

Case 3 is not an error. "No change-request document exists" is a meaningful
statement: it means nobody asked for the destination to change. That is
expressed as intent=PAYMENT_FOLLOWUP with evidence_source="no_document_supplied"
and handed to the SAME decision engine as everything else, where rule R2 already
encodes exactly the right policy:

  destination is a known account   -> R2a ALLOW    routine payout, nothing changed
  destination is unseen            -> R2b STEP_UP  money moving somewhere new
                                                    with nothing authorising it
  destination is another vendor's  -> R2c BLOCK

The handler therefore does not decide anything. It gathers evidence and calls
decide(), per the project rule that the decision engine is the only thing that
decides.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from decision_engine import FAVResult, VendorRecord, decide
from extractor import (
    ACTION_NONE, INTENT_FOLLOWUP, SCOPE_NONE, ExtractionResult,
)
import extractor as extractor_mod
import casefile
import notifier
import verifier

log = logging.getLogger("basedrift.webhook")

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# Razorpay retries a failed delivery for up to 24 hours. A freshness window
# SHORTER than the provider's retry period is a self-inflicted outage: a
# transient failure at minute 0 means every retry arrives "too old" and the
# payout is never decided at all — worse than the replay it was guarding
# against, because a stuck payout is invisible.
#
# Idempotency, not freshness, is the real replay control: an event already
# processed is refused however fresh it looks. The window is a coarse backstop
# for events far outside any legitimate retry, so it sits beyond the retry
# period rather than inside it.
RAZORPAY_RETRY_WINDOW_SECONDS = 24 * 60 * 60
MAX_EVENT_AGE_SECONDS = RAZORPAY_RETRY_WINDOW_SECONDS + 60 * 60

# How far back to look for a change-request document for this vendor.
CORRELATION_WINDOW_DAYS = 30


# ── Signature verification ────────────────────────────────────────────

def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    HMAC-SHA256 over the RAW request body, per Razorpay's webhook spec.

    Three details that are each a real vulnerability if got wrong:

    1. `raw_body` must be the bytes as received. Re-serialising the parsed JSON
       and signing that is the classic mistake: key order, whitespace and
       unicode escaping all change, so either every valid event is rejected or —
       far worse, if the comparison is later "fixed" by loosening it — forged
       ones are accepted.
    2. The comparison is constant-time. A byte-by-byte `==` leaks how much of a
       guessed signature was correct, which is enough to forge one.
    3. Absent or malformed input returns False. It never falls through to True.
    """
    if not signature or not secret or raw_body is None:
        return False
    try:
        expected = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
    except (TypeError, AttributeError):
        return False
    return hmac.compare_digest(expected, signature.strip())


def webhook_secret() -> str:
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()


# ── Event model ───────────────────────────────────────────────────────

@dataclass
class PayoutEvent:
    event_id:        str
    event:           str
    created_at:      int
    payout_id:       str
    fund_account_id: str
    amount_paise:    Optional[int]
    notes:           Dict[str, Any] = field(default_factory=dict)

    @property
    def amount_rupees(self) -> Optional[float]:
        # Razorpay amounts are in paise. Treating them as rupees would compare
        # a 100x-inflated figure against the vendor's baseline.
        return None if self.amount_paise is None else self.amount_paise / 100.0


def parse_payout_pending(body: Dict[str, Any]) -> PayoutEvent:
    """Raises ValueError on anything that is not a well-formed payout event."""
    event = body.get("event")
    if event != "payout.pending":
        raise ValueError(f"unsupported event {event!r}")
    try:
        entity = body["payload"]["payout"]["entity"]
    except (KeyError, TypeError):
        raise ValueError("payload.payout.entity missing")

    payout_id = entity.get("id")
    fund_account_id = entity.get("fund_account_id")
    if not payout_id or not fund_account_id:
        raise ValueError("payout id or fund_account_id missing")

    return PayoutEvent(
        event_id=body.get("id") or f"evt_{payout_id}",
        event=event,
        created_at=int(body.get("created_at") or 0),
        payout_id=payout_id,
        fund_account_id=fund_account_id,
        amount_paise=entity.get("amount"),
        notes=entity.get("notes") or {},
    )


# ── Merchant-side stores ──────────────────────────────────────────────
# In production these are RazorpayX API reads and the merchant's own document
# store. Kept behind small interfaces so the handler logic is testable without
# a network, and so nothing here pretends to be a live Razorpay call.

@dataclass
class FundAccount:
    fund_account_id: str
    account_number:  str
    ifsc:            str
    contact_id:      str
    vendor_id:       str


class Store:
    """Fund accounts, vendors and change-request documents."""

    def __init__(self, audit_limit: int = 200, inbox=None):
        self.fund_accounts: Dict[str, FundAccount] = {}
        self.vendors: Dict[str, VendorRecord] = {}
        self.documents: Dict[str, Dict[str, Any]] = {}
        # The merchant's mailbox, behind the read-only MCP tool boundary. None
        # means no inbox is connected, and everything still works — a payout
        # then decides on the vendor master and the document alone, exactly as
        # it did before triage existed. Inbox evidence is corroborating; the
        # system must not depend on having it.
        self.inbox = inbox
        self._triaged: set = set()
        # What triage decided about each message, including the ones it DROPPED.
        # The queue of routed messages is the operator's work; the dropped ones
        # are how anyone checks that the funnel is not quietly discarding real
        # requests — which is the failure this whole layer is most exposed to.
        self.triage_log: Deque[Dict[str, Any]] = deque(maxlen=audit_limit * 5)
        self._seen_events: Dict[str, float] = {}
        # Decisions were computed and then discarded once the response was
        # written, which left nothing for an operator to review. Bounded and
        # in-memory, consistent with every other store here; production wants
        # durable append-only storage, since an audit trail that a restart
        # erases is not an audit trail.
        self.audits: Deque[Dict[str, Any]] = deque(maxlen=audit_limit)
        # What a human did about each held payout, keyed by payout id.
        # An append-only log rather than a status field: the state is
        # recomputed from it, so a case can never claim a status its
        # own history does not support. See src/casefile.py.
        self.case_actions: Dict[str, List[Dict[str, Any]]] = {}

    def case(self, payout_id: str) -> List[Dict[str, Any]]:
        """
        The case file for a payout. READING ONE DOES NOT CREATE IT.

        setdefault here meant every GET of a case page minted a case file,
        including for payout ids that do not exist — free unbounded growth on an
        endpoint with no authentication in front of it. Writers call
        record_case_action(), which creates the entry only after checking that a
        decision exists to work on.
        """
        return self.case_actions.get(payout_id, [])

    def record_case_action(self, payout_id: str, action: str, actor: str,
                           note: str = "", detail: str = "") -> Dict[str, Any]:
        """
        Record one human act against a payout, subject to the case rules.

        The guards live in casefile and are checked HERE, on the server, not in
        the template that draws the button. A greyed-out control stops an honest
        mistake; only this stops someone who crafts the request by hand, and the
        two-person rule is worth nothing if it can be skipped with curl.
        """
        a = self.find_audit(payout_id)
        if a is None:
            # No decision, no case to work. Without this, posting to an
            # arbitrary payout id would mint a case file for a payout that
            # does not exist — on an endpoint that has no authentication in
            # front of it, that is unbounded growth for free. See COMPLIANCE.md
            # section 3.
            raise PermissionError(
                "No decision has been recorded for that payout, so there is "
                "nothing to work.")
        actions = self.case_actions.setdefault(payout_id, [])
        final = a.get("final_outcome")
        if action == "released":
            ok, why = casefile.may_release(actions, actor, final)
            if not ok:
                raise PermissionError(why)
        elif action == "rejected":
            ok, why = casefile.may_reject(actions, final)
            if not ok:
                raise PermissionError(why)
        elif casefile.state_of(actions, final) in ("released", "rejected"):
            raise PermissionError(
                "This case is closed. Nothing further can be recorded on it.")
        entry = casefile.record(actions, action, actor, note=note, detail=detail)

        # Tell the merchant's own systems, AFTER the record exists. A case is
        # resolved because it is written down, not because a POST succeeded —
        # so notify() cannot raise and its result is deliberately ignored. See
        # src/notifier.py.
        if action in casefile.RESOLUTIONS:
            try:
                notifier.notify(payout_id, a, list(actions), action, actor)
            except Exception as e:                             # noqa: BLE001
                # notify() already swallows delivery failures. This catches
                # everything BEFORE the POST — building the event, serialising
                # an audit that turns out to hold something json cannot encode
                # — because the guarantee belongs to the caller, not to the
                # notifier's good intentions. A test kills a release by making
                # notify() itself raise, and it must not.
                log.warning("case notification for %s raised: %s", payout_id, e)
        return entry

    def record_audit(self, audit: Dict[str, Any]) -> None:
        self.audits.appendleft(audit)

    def recent_audits(self, limit: int = 50):
        return list(self.audits)[:limit]

    def find_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        for t in self.triage_log:
            if t.get("message_id") == message_id:
                return t
        return None

    def audit_for_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """The payout a routed message ended up deciding, if one has arrived."""
        for a in self.audits:
            if (a.get("document") or {}).get("document_id") == document_id:
                return a
        return None

    def find_audit(self, payout_id: str) -> Optional[Dict[str, Any]]:
        for a in self.audits:
            if a.get("payout_id") == payout_id:
                return a
        return None

    # -- fund accounts -------------------------------------------------
    def resolve_fund_account(self, fund_account_id: str) -> Optional[FundAccount]:
        """
        Production: GET /v1/fund_accounts/{id}.

        This is what closes P0.3 — the destination comes from the payout's own
        fund account, never from an account number quoted in the request.
        """
        return self.fund_accounts.get(fund_account_id)

    def account_index(self):
        """
        {account_number: set(vendor_ids)} for the cross-contact reuse check.

        A set, because an account legitimately belongs to more than one contact
        inside a corporate group. Assigning a single owner in a loop made the
        last writer win and rejected the group.
        """
        idx = {}
        for v in self.vendors.values():
            for acct in v.all_known_accounts():
                idx.setdefault(acct, set()).add(v.vendor_id)
        return idx

    # -- documents -----------------------------------------------------
    def put_document(self, doc_id: str, vendor_id: str, text: str,
                     received_at: Optional[float] = None,
                     inbox_signals=None, triage=None) -> None:
        """
        inbox_signals are gathered WHEN THE MESSAGE ARRIVES, not when the payout
        does, and stored with the document.

        That ordering is the point. "Has this sender written before?" has to be
        answered against the mailbox as it was at arrival; asking days later,
        when the payout finally goes pending, would count mail that landed in
        between and make a first contact look established.
        """
        self.documents[doc_id] = {
            "document_id": doc_id,
            "vendor_id": vendor_id,
            "text": text,
            "received_at": received_at if received_at is not None else time.time(),
            "inbox_signals": list(inbox_signals or []),
            "triage": triage,
        }

    def ingest_message(self, message) -> Dict[str, Any]:
        """
        The real front door: a raw message, not a document with a vendor id
        already attached.

        Triage resolves the vendor with no model call, and only messages that
        reach ROUTE become documents. Everything else is dropped here and never
        costs an extraction — which is the whole argument for the funnel.

        A dropped message is NOT a released payout. The payout.pending webhook
        fires regardless, finds no document, and R2 rules on the real
        destination: a known account passes, an unseen one holds, another
        vendor's account still fires R2c. The failure mode of this door is an
        unnecessary hold.
        """
        import triage as triage_mod

        if message.message_id in self._triaged:
            return {"verdict": triage_mod.DUPLICATE, "document_id": None}
        result = triage_mod.triage(message, self.vendors, self._triaged)

        def _log(document_id=None, signals=()):
            self.triage_log.appendleft({
                "message_id": message.message_id,
                "from": message.from_addr,
                "subject": message.subject,
                # The message itself. An operator asked to judge a payment
                # cannot do it from a verdict and a rule name — they have to be
                # able to read what the supplier actually wrote.
                "body": message.body,
                "thread_id": message.thread_id,
                "in_reply_to": message.in_reply_to,
                "received_at": message.received_at,
                "verdict": result.verdict,
                "stage": result.stage,
                "reason": result.reason,
                "vendor_id": result.vendor_id,
                "match": result.match,
                "matched_domain": result.matched_domain,
                "document_id": document_id,
                "inbox_signals": [s.name for s in signals],
                # And what each one actually found. The name alone made
                # every row read the same; the finding is what tells an
                # operator whether a sender has moved the destination
                # twice or eleven times.
                "inbox_findings": [
                    {"name": s.name, "result": s.result,
                     "detail": s.detail, "source": s.source}
                    for s in signals],
            })

        if not result.routed:
            _log()
            return {"verdict": result.verdict, "stage": result.stage,
                    "reason": result.reason, "document_id": None}

        signals = []
        if self.inbox is not None:
            import investigator
            inv = investigator.investigate(
                self.inbox, message, result,
                is_reply=bool(getattr(message, "in_reply_to", "")))
            signals = inv.signals

        doc_id = f"doc_{message.message_id.strip('<>').split('@')[0][:24]}"
        self.put_document(doc_id, result.vendor_id, message.body,
                          received_at=message.received_at or None,
                          inbox_signals=signals,
                          triage={"match": result.match,
                                  "matched_domain": result.matched_domain,
                                  "candidates": result.candidates})
        _log(document_id=doc_id, signals=signals)
        return {"verdict": result.verdict, "document_id": doc_id,
                "vendor_id": result.vendor_id, "match": result.match,
                "inbox_signals": [s.name for s in signals]}

    def find_document(self, vendor_id: str,
                      explicit_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if explicit_id:
            doc = self.documents.get(explicit_id)
            # An explicit reference pointing at another vendor's document is
            # not a near-miss to fall back from — it is a correlation failure,
            # and guessing a different document would be worse than holding.
            if doc and doc["vendor_id"] == vendor_id:
                return doc
            return None

        cutoff = time.time() - CORRELATION_WINDOW_DAYS * 86400
        candidates = [d for d in self.documents.values()
                      if d["vendor_id"] == vendor_id and d["received_at"] >= cutoff]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d["received_at"])

    # -- replay / idempotency ------------------------------------------
    def seen_before(self, event_id: str) -> bool:
        """
        In-memory, and therefore NOT sufficient for production: a restart
        forgets everything and a redelivery would be decided twice. Production
        needs a uniqueness constraint in durable storage, and the claim should
        not be marked complete until the work actually finishes — otherwise a
        crash mid-decision leaves an event permanently "processed" and the
        payout permanently pending.
        """
        now = time.time()
        for k, ts in list(self._seen_events.items()):
            if now - ts > MAX_EVENT_AGE_SECONDS * 2:
                del self._seen_events[k]
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = now
        return False


# ── The "no document" evidence object ─────────────────────────────────

def _accounts_on_file(vendor, destination: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every account the master holds for this vendor, with the four facts that
    decide whether it is established: when it was added, how, what verified it,
    and whether it has ever actually carried money.

    `established` is computed HERE from the same predicate decision_engine uses,
    rather than being restated in the template. A screen that draws its own
    conclusion about the evidence can disagree with the engine, and then the
    audit record and the operator are looking at two different systems.
    """
    out = []
    for a in getattr(vendor, "accounts", []) or []:
        verified = a.verified_by not in ("", "unverified")
        out.append({
            "account_number": a.account_number,
            "ifsc": a.ifsc,
            "status": a.status,
            "is_primary": a.is_primary,
            "is_destination": a.account_number == destination,
            "added_on": a.added_on,
            "added_via": a.added_via,
            "verified_by": a.verified_by,
            "settled_payout_count": a.settled_payout_count,
            "established": verified or a.settled_payout_count > 0,
        })
    return out


def _demand(vendor, doc) -> Dict[str, Any]:
    """
    The account a penny drop would have to come from, and why that one.

    Dated from the change request rather than from now: seasoning is measured
    against when the request arrived, so an account added after it must not
    count as established. Falls back to today only when there is no document.
    """
    as_of = time.strftime("%Y-%m-%d")
    if doc and doc.get("received_at"):
        as_of = time.strftime("%Y-%m-%d", time.gmtime(doc["received_at"]))
    account, basis = verifier.select_verification_account(vendor, as_of)
    return {
        "account_number": account.account_number if account else None,
        "basis": basis,
        "as_of": as_of,
        "available": account is not None,
    }


def no_document_evidence() -> ExtractionResult:
    """
    The absence of a change request is itself evidence, and a true statement:
    nobody asked for the destination to change.

    Marked with evidence_source so the audit trail can never confuse this with
    something a model read. R2 then applies the right policy — routine payouts
    to known accounts pass, and a payout to an unseen account with nothing
    authorising it is held.
    """
    return ExtractionResult(
        ok=True,
        intent=INTENT_FOLLOWUP, action=ACTION_NONE, scope=SCOPE_NONE,
        reasoning="No change-request document was supplied for this payout, so "
                  "no destination change has been requested.",
        evidence_source="no_document_supplied",
    )


# ── Handler ───────────────────────────────────────────────────────────

@dataclass
class HandlerResult:
    status:   int
    outcome:  str
    detail:   str
    audit:    Optional[Dict[str, Any]] = None
    actions:  List[Dict[str, Any]] = field(default_factory=list)


HOLD = "HELD"


def handle_payout_pending(raw_body: bytes,
                          signature: str,
                          store: Store,
                          secret: Optional[str] = None,
                          fav_lookup=None,
                          event_id_header: Optional[str] = None,
                          extract_fn=None) -> HandlerResult:
    """
    Full path from raw request to decision. Never raises.

    Every early return here leaves the payout pending, which is the safe state.
    """
    secret = webhook_secret() if secret is None else secret

    # 1. Authenticity, before the body is parsed or trusted in any way.
    if not secret:
        return HandlerResult(500, HOLD,
                             "RAZORPAY_WEBHOOK_SECRET is not configured; refusing "
                             "to process unauthenticated events")
    if not verify_signature(raw_body, signature, secret):
        return HandlerResult(400, HOLD, "signature verification failed")

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        return HandlerResult(400, HOLD, f"body is not valid JSON: {e}")

    try:
        evt = parse_payout_pending(body)
    except ValueError as e:
        return HandlerResult(400, HOLD, str(e))

    # 2. Replay window. A captured event with a valid signature stays valid
    #    forever otherwise.
    #
    #    A missing or zero created_at is REFUSED rather than waved through. An
    #    earlier version guarded this with `if evt.created_at:`, which is falsy
    #    at 0 and therefore skipped the freshness check on exactly the events
    #    whose age could not be established. Razorpay always sends the field, so
    #    requiring it costs nothing, and "cannot verify how old this is" is
    #    inconclusive — which never passes here.
    if evt.created_at <= 0:
        return HandlerResult(400, HOLD,
                             "event timestamp missing or zero; freshness cannot "
                             "be verified")
    age = time.time() - evt.created_at
    if age > MAX_EVENT_AGE_SECONDS:
        return HandlerResult(400, HOLD,
                             f"event is {int(age)}s old, beyond the "
                             f"{MAX_EVENT_AGE_SECONDS}s replay window")

    # 3. Idempotency. Razorpay retries; re-deciding is wasteful and re-emitting
    #    approve/reject calls is worse.
    # Razorpay's own x-razorpay-event-id header is authoritative for identity
    # and is what stays stable across retries of the same delivery. The body id
    # is a fallback for callers that do not send it.
    dedupe_key = (event_id_header or "").strip() or evt.event_id
    if store.seen_before(dedupe_key):
        return HandlerResult(200, "DUPLICATE",
                             f"event {dedupe_key} already processed")

    # 4. The authoritative destination — from the payout, never from a document.
    fa = store.resolve_fund_account(evt.fund_account_id)
    if fa is None:
        return HandlerResult(200, HOLD,
                             f"fund account {evt.fund_account_id} could not be "
                             f"resolved; payout left pending")

    vendor = store.vendors.get(fa.vendor_id)
    if vendor is None:
        return HandlerResult(200, HOLD,
                             f"fund account {evt.fund_account_id} maps to unknown "
                             f"vendor {fa.vendor_id}; payout left pending")

    # 5. Correlate the change-request document.
    explicit = evt.notes.get("basedrift_document_id")
    doc = store.find_document(vendor.vendor_id, explicit)

    if doc is None:
        ext = no_document_evidence()
        doc_id = None
        inbox_signals = []
    else:
        # Injected the same way fav_lookup is, and for the same reason: the
        # real extractor costs an API call per payout, so anything replaying a
        # queue — the demo, an evaluator, a test — needs to supply the reading
        # rather than buy it. Default is the live model, so nothing changes for
        # the actual webhook path.
        ext = (extract_fn or extractor_mod.extract)(doc["text"])
        doc_id = doc["document_id"]
        # Gathered at arrival and carried here. See Store.put_document.
        inbox_signals = doc.get("inbox_signals") or []

    # 6. FAV, replayed schema-faithfully. Unavailable resolves to inconclusive,
    #    which the engine already refuses to treat as clean.
    fav = (fav_lookup(fa) if fav_lookup
           else FAVResult(account_status="unknown", registered_name=None,
                          name_match_score=None))

    # 7. The decision engine decides. The handler does not.
    dec = decide(ext, fav, vendor,
                 other_vendor_accounts=store.account_index(),
                 destination_account_number=fa.account_number,
                 vendors=store.vendors,
                 inbox_signals=inbox_signals)

    # Neither channel is attempted inline. The callback is a phone call and the
    # penny drop is a live RazorpayX call that the vendor has to act on — both
    # take minutes to days, and the webhook has seconds. The payout simply stays
    # pending until a channel resolves it out of band, which is the safe state
    # anyway. requester_controls_accounts=None means channel 2 was not attempted
    # — not that it failed, and not that it passed.
    ver = verifier.verify(dec, vendor, callback_reaches_known_contact=False,
                          case_id=evt.payout_id,
                          requester_controls_accounts=None)
    final = ver.final_outcome if ver else dec.outcome
    actions = verifier.razorpay_actions(
        final, ver.reason if ver else dec.reason,
        evt.payout_id, evt.fund_account_id,
        recommended_action=dec.recommended_action,
    )

    audit = {
        "event_id": evt.event_id,
        "payout_id": evt.payout_id,
        "vendor_id": vendor.vendor_id,
        "destination": {
            "fund_account_id": evt.fund_account_id,
            "account_number": fa.account_number,
            "source": "razorpay_fund_account",
        },
        # HOW EACH ACCOUNT ON FILE GOT THERE, which is what _unanchored()
        # decides on. The operator was being shown a verdict about the vendor
        # master without being shown the part of the master the verdict turned
        # on — so "on file but never established" was a sentence they had to
        # take on trust. This is deliberately NOT a browsable vendor master:
        # it is the evidence behind this decision, on this payout. The master
        # is the thing being attacked, and a screen inviting someone to eyeball
        # it and conclude the request looks fine is the reasoning the planted-
        # account case exists to refute.
        "accounts_on_file": _accounts_on_file(vendor, fa.account_number),
        "document": {
            "document_id": doc_id,
            "correlation": ("explicit_note" if explicit and doc
                            else "recent_for_vendor" if doc else "none_found"),
        },
        "amount_rupees": evt.amount_rupees,
        "extraction": ext.to_dict(),
        "decision": dec.to_dict(),
        "verification": ver.to_dict() if ver else None,
        # WHAT WOULD RELEASE THIS, computed for the operator rather than for
        # the handler. The webhook never attempts channel 2 — a penny drop
        # takes days and the request has seconds — so verification_account is
        # empty on this path. But the person who WILL make that call needs to
        # know which account to demand, and computing it only when the channel
        # already ran meant the answer existed everywhere except where someone
        # could act on it.
        "verification_demand": _demand(vendor, doc),
        "final_outcome": final,
        "payout_allowed": ver.payout_allowed if ver else dec.payout_allowed,
        "razorpay_actions": actions,
    }
    store.record_audit(audit)
    return HandlerResult(200, final, dec.reason, audit=audit, actions=actions)


# ── FastAPI surface ───────────────────────────────────────────────────

def create_app(store: Optional[Store] = None, fav_lookup=None):
    """
    Built lazily so importing this module costs nothing when only the pure
    functions above are needed — the tests exercise those without a server.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    import dashboard

    app = FastAPI(title="BaseDrift", version="1.0")
    app.state.store = store if store is not None else Store()

    @app.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request):
        # The RAW bytes, before any parsing. Signing a re-serialised body is
        # the classic way to break this.
        raw = await request.body()
        sig = request.headers.get(SIGNATURE_HEADER, "")
        result = handle_payout_pending(
            raw, sig, app.state.store, fav_lookup=fav_lookup,
            event_id_header=request.headers.get(EVENT_ID_HEADER))
        if result.audit:
            log.info("payout %s -> %s", result.audit["payout_id"], result.outcome)
        # Nothing identifying goes back over HTTP. The audit record obviously
        # cannot, but neither can `detail` once a decision was reached: it is
        # the rule's reason string, and those embed the destination account
        # number and the vendor's known accounts. Razorpay needs to know the
        # event was accepted; everything else belongs in server-side storage.
        #
        # Rejection paths have no audit and carry only static, non-identifying
        # messages ("signature verification failed"), which are safe to return
        # and genuinely useful when debugging a delivery.
        payload = {"outcome": result.outcome}
        if result.audit:
            payload["payout_id"] = result.audit["payout_id"]
        else:
            payload["detail"] = result.detail
        return JSONResponse(status_code=result.status, content=payload)

    @app.post("/documents")
    async def ingest_document(request: Request):
        """
        Where the merchant's AP system posts a change-request document.

        This is the second input the webhook cannot supply. Returns the id to
        put in the payout's notes.basedrift_document_id for exact correlation.
        """
        body = await request.json()
        doc_id = body.get("document_id") or f"doc_{int(time.time()*1000)}"
        vendor_id, text = body.get("vendor_id"), body.get("text")
        if not vendor_id or not text:
            return JSONResponse(status_code=400,
                                content={"error": "vendor_id and text are required"})
        app.state.store.put_document(doc_id, vendor_id, text)
        return {"document_id": doc_id, "vendor_id": vendor_id}

    @app.post("/messages")
    async def ingest_message(request: Request):
        """
        Where the merchant's mailbox delivers. Unlike /documents this takes a
        RAW message with no vendor id attached — resolving the vendor is
        triage's job, and getting it wrong is survivable because the payout,
        not the email, is what the decision engine trusts for identity.
        """
        import triage as triage_mod
        body = await request.json()
        if not body.get("body"):
            return JSONResponse(status_code=400,
                                content={"error": "body is required"})
        msg = triage_mod.Message(
            message_id=body.get("message_id") or f"<{int(time.time()*1000)}@in>",
            from_addr=body.get("from", ""),
            subject=body.get("subject", ""),
            body=body["body"],
            thread_id=body.get("thread_id", ""),
            received_at=float(body.get("received_at") or time.time()),
            headers=body.get("headers") or {},
            in_reply_to=body.get("in_reply_to", ""),
        )
        return app.state.store.ingest_message(msg)

    # ── Operator dashboard ────────────────────────────────────────────
    # Deliberately shows what the webhook response does not. See the note at
    # the top of dashboard.py: different audience, and it needs auth in front
    # of it wherever this is actually deployed.

    @app.get("/", response_class=HTMLResponse)
    async def decisions():
        # The whole queue, not the first 50. recent_audits() defaults to a
        # page size that made sense when a demo produced three decisions; with
        # a replayed morning's mail it silently truncated the list, so the
        # decision count on screen disagreed with the inbox beside it.
        return dashboard.render_index(
            app.state.store.recent_audits(limit=len(app.state.store.audits)))

    def _decorate(t):
        """A triage row plus the payout decision it produced, if one arrived."""
        store = app.state.store
        row = dict(t)
        if t.get("document_id"):
            a = store.audit_for_document(t["document_id"])
            if a:
                d = a.get("decision") or {}
                row["payout_id"] = a.get("payout_id")
                row["final_outcome"] = a.get("final_outcome")
                row["rule_fired"] = d.get("rule_fired")
                row["recommended_action"] = d.get("recommended_action")
                row["audit"] = a
                # Where the HUMAN work stands, which is what an operator is
                # actually queueing on. "Held" and "held, called, waiting on a
                # second person" are different jobs and looked identical.
                row["case"] = casefile.summary(
                    store.case(a.get("payout_id")), a.get("final_outcome"))
        return row

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox():
        return HTMLResponse(dashboard.render_inbox(
            [_decorate(t) for t in app.state.store.triage_log]))

    @app.get("/message/{message_id:path}", response_class=HTMLResponse)
    async def message(message_id: str):
        t = app.state.store.find_message(message_id)
        return HTMLResponse(dashboard.render_message(
            _decorate(t) if t else None))

    def _actor(request) -> str:
        """
        Who the dashboard is acting as.

        A cookie, and deliberately not authentication. This demo has none —
        COMPLIANCE.md lists it as production work — so the identity is chosen,
        not proven. What it does buy is that the two-person rule is exercised
        for real: the same browser cannot record a verification and then release
        the payment without switching, and switching is a visible act.
        """
        name = request.cookies.get("pp_actor") or casefile.OPERATORS[0][0]
        return name

    def _case_page(payout_id: str, actor: str, error: str = ""):
        store = app.state.store
        return dashboard.render_case(
            store.find_audit(payout_id),
            case=store.case(payout_id) if payout_id else None,
            actor=actor, error=error)

    @app.get("/case/{payout_id}", response_class=HTMLResponse)
    async def case_detail(payout_id: str, request: Request):
        return HTMLResponse(_case_page(payout_id, _actor(request)))

    @app.post("/case/{payout_id}/action", response_class=HTMLResponse)
    async def case_action(payout_id: str, request: Request):
        form = await request.form()
        actor = (form.get("actor") or _actor(request)).strip()
        action = (form.get("action") or "").strip()
        note = (form.get("note") or "").strip()
        detail = (form.get("detail") or "").strip()
        error = ""
        try:
            app.state.store.record_case_action(
                payout_id, action, actor, note=note, detail=detail)
        except (PermissionError, ValueError) as e:
            error = str(e)
        r = HTMLResponse(_case_page(payout_id, actor, error))
        # Remember who is acting, so the next screen does not silently revert
        # to somebody else and make the two-person rule look like a glitch.
        r.set_cookie("pp_actor", actor, httponly=True, samesite="lax")
        return r

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "webhook_secret_configured": bool(webhook_secret())}

    return app
