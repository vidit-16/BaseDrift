"""
PayeeProof — payout.pending webhook handler.

This is the integration point the README's first paragraph describes: RazorpayX
fires `payout.pending` while the payout is frozen and no money has moved, and
PayeeProof decides before it thaws.

FAIL-SAFE BY CONSTRUCTION
=========================
The safe state is INACTION. A pending payout stays pending until something
explicitly approves it, so every failure mode here — bad signature, unknown
fund account, unknown vendor, missing document, crashed process, PayeeProof
being down entirely — leaves the payout held. There is no code path where an
error releases money. That is why the handler never "defaults to approve" on
anything, and why a 500 is an acceptable outcome: Razorpay retries, and the
payout is safe in the meantime.

THE CORRELATION PROBLEM, AND WHY IT IS NOT HAND-WAVED
=====================================================
A payout.pending event tells you money is about to move. It does NOT tell you
what document requested that destination — and PayeeProof's whole question is
about the provenance of a change request. Two sources are needed, and only one
arrives on the webhook.

The merchant's AP system supplies the other, via POST /documents. Correlation
then runs in this order:

  1. payout.notes.payeeproof_document_id  — an explicit reference, unambiguous
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
import verifier

log = logging.getLogger("payeeproof.webhook")

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

    def __init__(self, audit_limit: int = 200):
        self.fund_accounts: Dict[str, FundAccount] = {}
        self.vendors: Dict[str, VendorRecord] = {}
        self.documents: Dict[str, Dict[str, Any]] = {}
        self._seen_events: Dict[str, float] = {}
        # Decisions were computed and then discarded once the response was
        # written, which left nothing for an operator to review. Bounded and
        # in-memory, consistent with every other store here; production wants
        # durable append-only storage, since an audit trail that a restart
        # erases is not an audit trail.
        self.audits: Deque[Dict[str, Any]] = deque(maxlen=audit_limit)

    def record_audit(self, audit: Dict[str, Any]) -> None:
        self.audits.appendleft(audit)

    def recent_audits(self, limit: int = 50):
        return list(self.audits)[:limit]

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
                     received_at: Optional[float] = None) -> None:
        self.documents[doc_id] = {
            "document_id": doc_id,
            "vendor_id": vendor_id,
            "text": text,
            "received_at": received_at if received_at is not None else time.time(),
        }

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
                          event_id_header: Optional[str] = None) -> HandlerResult:
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
    explicit = evt.notes.get("payeeproof_document_id")
    doc = store.find_document(vendor.vendor_id, explicit)

    if doc is None:
        ext = no_document_evidence()
        doc_id = None
    else:
        ext = extractor_mod.extract(doc["text"])
        doc_id = doc["document_id"]

    # 6. FAV, replayed schema-faithfully. Unavailable resolves to inconclusive,
    #    which the engine already refuses to treat as clean.
    fav = (fav_lookup(fa) if fav_lookup
           else FAVResult(account_status="unknown", registered_name=None,
                          name_match_score=None))

    # 7. The decision engine decides. The handler does not.
    dec = decide(ext, fav, vendor,
                 other_vendor_accounts=store.account_index(),
                 destination_account_number=fa.account_number,
                 vendors=store.vendors)

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
        "document": {
            "document_id": doc_id,
            "correlation": ("explicit_note" if explicit and doc
                            else "recent_for_vendor" if doc else "none_found"),
        },
        "amount_rupees": evt.amount_rupees,
        "extraction": ext.to_dict(),
        "decision": dec.to_dict(),
        "verification": ver.to_dict() if ver else None,
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

    app = FastAPI(title="PayeeProof", version="1.0")
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
        put in the payout's notes.payeeproof_document_id for exact correlation.
        """
        body = await request.json()
        doc_id = body.get("document_id") or f"doc_{int(time.time()*1000)}"
        vendor_id, text = body.get("vendor_id"), body.get("text")
        if not vendor_id or not text:
            return JSONResponse(status_code=400,
                                content={"error": "vendor_id and text are required"})
        app.state.store.put_document(doc_id, vendor_id, text)
        return {"document_id": doc_id, "vendor_id": vendor_id}

    # ── Operator dashboard ────────────────────────────────────────────
    # Deliberately shows what the webhook response does not. See the note at
    # the top of dashboard.py: different audience, and it needs auth in front
    # of it wherever this is actually deployed.

    @app.get("/", response_class=HTMLResponse)
    async def decisions():
        return dashboard.render_index(app.state.store.recent_audits())

    @app.get("/case/{payout_id}", response_class=HTMLResponse)
    async def case_detail(payout_id: str):
        return dashboard.render_case(app.state.store.find_audit(payout_id))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "webhook_secret_configured": bool(webhook_secret())}

    return app
