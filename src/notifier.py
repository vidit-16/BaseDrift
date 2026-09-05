"""
BaseDrift — the outbound notification, fired when a case resolves.

The inbound webhook is Razorpay telling us a payout is pending. This is the
other direction: BaseDrift telling the merchant's own systems that a held
payout has been released or refused, and by whom. Without it the ERP has no way
to learn the outcome except by somebody watching the dashboard.

WHY ONLY ON RESOLUTION
A case moves through several states — contacted, awaiting proof, verified — and
none of those is a fact another system can act on. `released` and `rejected` are
terminal and they are what an ERP reconciles against. Emitting the intermediate
states would be a feed nobody consumes.

THREE RULES THIS FILE FOLLOWS
=============================

**1. Delivery never affects the decision.** The case is already resolved and
recorded before anything is sent. A timeout, a 500, a misconfigured URL, a DNS
failure — none of them may raise, retry into the request, or roll anything back.
The whole system's safe state is inaction, and a notification that could undo a
release by failing would invert that.

**2. It is signed, with the same scheme we require of Razorpay.** HMAC-SHA256
over the exact bytes sent, in X-BaseDrift-Signature. A receiver that cannot
authenticate the sender cannot act on this, and telling somebody's finance
system "this payout was released" is worth forging. We verify signatures on the
way in for exactly this reason; sending unsigned would be asking of others what
we refuse ourselves.

**3. Off unless configured.** No URL, no notification, no error. A control layer
that starts talking to the network because someone imported it is not a control
layer.

WHAT PRODUCTION WOULD ADD
A durable queue. This posts once, inline, with a short timeout, and a failed
delivery is logged and dropped — the audit trail is still authoritative, so
nothing is lost that cannot be re-read, but the receiver misses the event. At-
least-once delivery with retries and an idempotency key on the receiving side is
the real shape, and the event id below is already there to dedupe on.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("basedrift.notifier")

TIMEOUT_SECONDS = 5


def endpoint() -> Optional[str]:
    return os.environ.get("BASEDRIFT_WEBHOOK_URL") or None


def secret() -> Optional[str]:
    return os.environ.get("BASEDRIFT_WEBHOOK_SECRET") or None


def sign(raw: bytes, key: str) -> str:
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def build_event(payout_id: str, audit: Dict[str, Any],
                actions: List[Dict[str, Any]], resolution: str,
                actor: str, at: Optional[float] = None) -> Dict[str, Any]:
    """
    What the merchant's systems need to close the loop, and nothing more.

    Shaped like the events Razorpay sends us — id, entity, event, created_at,
    payload — because the receiving end is a team who already parse those, and
    inventing a second envelope for no reason costs them a day.

    It carries the destination account. That is not a disclosure: it is the
    merchant's own payout, to their own vendor, going to their own configured
    endpoint. What it is NOT allowed to carry is anything the dashboard would
    not show the same people.
    """
    decision = (audit or {}).get("decision") or {}
    at = time.time() if at is None else at
    return {
        "id": f"evt_pp_{payout_id}_{int(at)}",
        "entity": "event",
        "event": f"basedrift.case.{resolution}",
        "created_at": int(at),
        "payload": {
            "case": {
                "payout_id": payout_id,
                "vendor_id": (audit or {}).get("vendor_id"),
                "resolution": resolution,
                "resolved_by": actor,
                "resolved_at": at,
                "engine_outcome": (audit or {}).get("final_outcome"),
                "rule_fired": decision.get("rule_fired"),
                "recommended_action": decision.get("recommended_action"),
                "destination_account_number": (
                    (audit or {}).get("destination") or {}
                ).get("account_number"),
                # Who established what, in order. This is the part an auditor
                # asks for later and the part a reconciliation cannot
                # reconstruct on its own.
                "history": [
                    {"action": a.get("action"), "actor": a.get("actor"),
                     "at": a.get("at"), "note": a.get("note"),
                     "detail": a.get("detail")}
                    for a in actions
                ],
            }
        },
    }


def notify(payout_id: str, audit: Dict[str, Any],
           actions: List[Dict[str, Any]], resolution: str, actor: str,
           post=None) -> Optional[Dict[str, Any]]:
    """
    Send it, or don't, and never let that decide anything.

    Returns the event that was sent, or None when no endpoint is configured.
    `post` is injected so the tests can assert on what would go over the wire
    without one going over the wire.
    """
    url = endpoint()
    if not url:
        return None

    event = build_event(payout_id, audit, actions, resolution, actor)
    raw = json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}

    key = secret()
    if key:
        headers["X-BaseDrift-Signature"] = sign(raw, key)
    else:
        # Loud, because an unsigned notification is one the receiver cannot
        # safely act on, and silence here would let a deployment run that way
        # for months without anyone noticing.
        log.warning("BASEDRIFT_WEBHOOK_URL is set without "
                    "BASEDRIFT_WEBHOOK_SECRET; sending unsigned")

    try:
        if post is None:
            import requests
            post = requests.post
        post(url, data=raw, headers=headers, timeout=TIMEOUT_SECONDS)
    except Exception as e:                                     # noqa: BLE001
        # Deliberately broad, and deliberately swallowed. The case is already
        # resolved and written down; the notification is a convenience for
        # another system. Nothing about a failed POST may reach the caller.
        log.warning("case notification for %s failed: %s", payout_id, e)
    return event
