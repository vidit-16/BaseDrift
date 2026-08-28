"""
ASGI entry point.

    set RAZORPAY_WEBHOOK_SECRET=whsec_...
    uvicorn webhook_app:app --app-dir src --port 8000

Then:
    http://localhost:8000/          decisions, newest first
    http://localhost:8000/healthz   liveness

THE STORE STARTS EMPTY ON PURPOSE
=================================
An empty store resolves no fund accounts, so every payout is held. That is the
correct default for a control whose safe state is inaction — a misconfigured
deployment that has not been given vendor data must not start approving things.
Seeding it with real vendor and fund-account data is the merchant's integration
step, not something this file should guess at.

FOR A DEMO
==========
    set PAYEEPROOF_SEED_DEMO=1

Loads the five scenarios from webhook_demo.py and decides them at startup, so
the dashboard has something in it when it opens. Off by default and never a
fallback: the flag has to be set deliberately, because a store that quietly
populated itself with fixtures would be exactly the kind of "helpful" default
that makes a control untrustworthy.
"""

import hashlib
import hmac
import json
import os
import time

from webhook import Store, create_app, handle_payout_pending


def _seed(store, secret):
    """Decide the demo scenarios so the dashboard opens with real records."""
    import webhook_demo as demo

    for fa in ("fa_usual", "fa_new", "fa_mule"):
        body = {
            "id": f"evt_seed_{fa}", "entity": "event",
            "event": "payout.pending", "contains": ["payout"],
            "created_at": int(time.time()),
            "payload": {"payout": {"entity": {
                "id": f"pout_{fa}", "entity": "payout",
                "fund_account_id": fa, "amount": 2800000,
                "currency": "INR", "status": "pending", "notes": {},
            }}},
        }
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        handle_payout_pending(raw, sig, store, secret=secret,
                              event_id_header=body["id"])
    return demo


if os.environ.get("PAYEEPROOF_SEED_DEMO") == "1":
    import webhook_demo as _demo

    _secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET") or _demo.SECRET
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = _secret
    store = _demo.build_store()
    _seed(store, _secret)
else:
    store = Store()

app = create_app(store)
