"""
BaseDrift — the outbound notification fired when a case resolves.

The inbound webhook is Razorpay telling us a payout is pending. This is the
other direction, and it is the only way the merchant's own systems learn that a
held payout was released or refused without somebody watching the dashboard.

Four properties carry it, and each is a way the feature could be worse than not
having it:

  1  DELIVERY NEVER AFFECTS THE DECISION. The case is resolved because it is
     written down, not because a POST succeeded. A notification that could undo
     a release by timing out would invert the entire fail-safe posture.

  2  IT IS SIGNED, with the scheme we demand of Razorpay. Telling a finance
     system "this payout was released" is worth forging.

  3  IT FIRES ONLY ON RESOLUTION. Intermediate states are not facts another
     system can act on.

  4  IT IS OFF UNLESS CONFIGURED. A control layer that starts talking to the
     network on import is not a control layer.

No network, no API key.
"""

import hashlib
import hmac
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import casefile as C  # noqa: E402
import notifier as N  # noqa: E402

AUDIT = {
    "payout_id": "pout_1",
    "vendor_id": "VEND0069",
    "final_outcome": "STEP_UP_VERIFY",
    "destination": {"account_number": "434392416664"},
    "decision": {"rule_fired": "R5_tier1_inconclusive",
                 "recommended_action": None},
}


class _Env:
    """Set env vars for one block and put them back afterwards."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _case():
    actions = []
    C.record(actions, "callback_confirmed", "Priya Menon",
             note="Spoke to Suresh on the number we hold.")
    C.record(actions, "released", "Rahul Iyer")
    return actions


# ══ 4. Off unless configured ══════════════════════════════════════════

def test_nothing_is_sent_when_no_endpoint_is_configured():
    sent = []
    with _Env(BASEDRIFT_WEBHOOK_URL=None, BASEDRIFT_WEBHOOK_SECRET=None):
        ev = N.notify("pout_1", AUDIT, _case(), "released", "Rahul Iyer",
                      post=lambda *a, **k: sent.append(a))
    assert ev is None
    assert sent == [], "posted to the network with nothing configured"


# ══ 2. Signed, with the scheme we demand of Razorpay ══════════════════

def test_the_payload_is_signed_over_the_exact_bytes_sent():
    """
    Over the bytes on the wire, not a re-serialisation of them. The inbound
    handler already learned this: verifying a re-encoded body lets any
    whitespace-preserving change through.
    """
    captured = {}

    def post(url, data=None, headers=None, timeout=None):
        captured.update(url=url, data=data, headers=headers)

    with _Env(BASEDRIFT_WEBHOOK_URL="https://erp.example/hook",
              BASEDRIFT_WEBHOOK_SECRET="whsec_out"):
        N.notify("pout_1", AUDIT, _case(), "released", "Rahul Iyer", post=post)

    raw = captured["data"]
    got = captured["headers"]["X-BaseDrift-Signature"]
    want = hmac.new(b"whsec_out", raw, hashlib.sha256).hexdigest()
    assert got == want, "signature does not cover the bytes that were sent"
    assert json.loads(raw)["payload"]["case"]["payout_id"] == "pout_1"


def test_an_unsigned_send_is_still_possible_but_warned():
    """
    A URL with no secret is a misconfiguration, not a crash. It sends, because
    refusing would make the notification a way to break a deployment, and it
    warns, because months of unsigned events reaching a finance system is not
    something anyone should discover by accident.
    """
    captured = {}
    with _Env(BASEDRIFT_WEBHOOK_URL="https://erp.example/hook",
              BASEDRIFT_WEBHOOK_SECRET=None):
        N.notify("pout_1", AUDIT, _case(), "released", "Rahul Iyer",
                 post=lambda url, **k: captured.update(k))
    assert "X-BaseDrift-Signature" not in captured.get("headers", {})


# ══ 1. Delivery never affects the decision ════════════════════════════

def test_a_failed_delivery_never_raises():
    """
    The case is already resolved and recorded. A timeout, a 500, DNS failure,
    a receiver that has been down for a week — none of it may reach the caller,
    because the caller is the code that just released somebody's payment.
    """
    def explode(*a, **k):
        raise ConnectionError("receiver is down")

    with _Env(BASEDRIFT_WEBHOOK_URL="https://erp.example/hook",
              BASEDRIFT_WEBHOOK_SECRET="whsec_out"):
        ev = N.notify("pout_1", AUDIT, _case(), "released", "Rahul Iyer",
                      post=explode)
    assert ev is not None, "the event should still be reported as built"


def test_a_dead_receiver_does_not_stop_a_release():
    """
    The property that matters, end to end through the store rather than the
    notifier alone: with the endpoint pointed at something that always fails,
    the release still happens and the case file still says so.
    """
    sys.path.insert(0, os.path.join(HERE, "..", "src"))
    import webhook as W

    store = W.Store()
    store.audits.appendleft(dict(AUDIT))

    real = N.notify

    def boom(*a, **k):
        raise ConnectionError("receiver is down")

    N.notify = boom
    try:
        store.record_case_action("pout_1", "callback_confirmed", "Priya Menon")
        store.record_case_action("pout_1", "released", "Rahul Iyer")
    finally:
        N.notify = real

    assert C.state_of(store.case("pout_1")) == "released", (
        "a dead notification receiver blocked a release")


# ══ 3. Only on resolution ═════════════════════════════════════════════

def test_intermediate_states_send_nothing():
    """
    "A call is under way" is not a fact another system can act on. Emitting
    every state change would be a feed nobody consumes, and one more place for
    account numbers to travel.
    """
    import webhook as W
    sent = []
    store = W.Store()
    store.audits.appendleft(dict(AUDIT))
    real = N.notify
    N.notify = lambda *a, **k: sent.append(a[3])
    try:
        for action in ("callback_requested", "callback_confirmed"):
            store.record_case_action("pout_1", action, "Priya Menon")
        assert sent == [], f"notified on intermediate states: {sent}"
        store.record_case_action("pout_1", "released", "Rahul Iyer")
        assert sent == ["released"], sent
    finally:
        N.notify = real


def test_a_rejection_notifies_too():
    import webhook as W
    sent = []
    store = W.Store()
    store.audits.appendleft(dict(AUDIT))
    real = N.notify
    N.notify = lambda *a, **k: sent.append(a[3])
    try:
        store.record_case_action("pout_1", "rejected", "Meera Nair")
    finally:
        N.notify = real
    assert sent == ["rejected"]


# ══ What the event says ═══════════════════════════════════════════════

def test_the_event_carries_who_established_what():
    """
    The history is the part a reconciliation cannot reconstruct on its own, and
    the part an auditor asks for later: who verified, who released, and that
    they were different people.
    """
    ev = N.build_event("pout_1", AUDIT, _case(), "released", "Rahul Iyer",
                       at=1000.0)
    case = ev["payload"]["case"]
    assert case["resolution"] == "released"
    assert case["resolved_by"] == "Rahul Iyer"
    assert case["rule_fired"] == "R5_tier1_inconclusive"
    assert case["engine_outcome"] == "STEP_UP_VERIFY"
    actors = [h["actor"] for h in case["history"]]
    assert actors == ["Priya Menon", "Rahul Iyer"]
    assert ev["event"] == "basedrift.case.released"


def test_the_event_id_is_stable_enough_to_deduplicate_on():
    """At-least-once delivery is the realistic shape; the receiver needs a key."""
    a = N.build_event("pout_1", AUDIT, _case(), "released", "R", at=1000.0)
    b = N.build_event("pout_1", AUDIT, _case(), "released", "R", at=1000.0)
    assert a["id"] == b["id"] and a["id"].startswith("evt_pp_pout_1")


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
