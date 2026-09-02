"""
PayeeProof — webhook handler tests.

Two halves:

  SECURITY   — a webhook that can be forged is worse than no webhook, because
               it hands an attacker the approve endpoint. These tests assert
               the negatives: forged, tampered, replayed and unsigned events
               must all be refused.

  FAIL-SAFE  — every failure path must leave the payout PENDING. There must be
               no input, malformed or otherwise, that causes a release.

No API key and no network needed: the store is in-memory and the only case that
would call the model uses a document short enough to be exercised separately.
"""

import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
# Repo root too: the inbox lives in the mcp/ package, and the triage-front-door
# tests import it before any helper has a chance to extend the path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from extractor import ExtractionResult  # noqa: E402

import casefile  # noqa: E402
import webhook  # noqa: E402
from decision_engine import ALLOW, BLOCK, STEP_UP, VendorRecord  # noqa: E402
from webhook import (  # noqa: E402
    FundAccount, Store, handle_payout_pending, no_document_evidence,
    parse_payout_pending, verify_signature,
)

SECRET = "whsec_test_do_not_use_in_production"
KNOWN_ACCT = "434392416664"
NEW_ACCT = "351349409853"
OTHER_ACCT = "999988887777"

VENDOR = VendorRecord(
    vendor_id="VEND0069", legal_name="Balaji Logistics",
    gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
    known_phone="9088190947", known_account_number=KNOWN_ACCT,
    known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
)
OTHER_VENDOR = VendorRecord(
    vendor_id="VEND0123", legal_name="Nova Packaging",
    gstin="27AAAAA0000A1Z5", known_domain="novapackaging.com",
    known_phone="9000000000", known_account_number=OTHER_ACCT,
    known_ifsc="HDFC0001234", avg_payout_amount=45000.0,
)


def make_store(dest_account=KNOWN_ACCT):
    s = Store()
    s.vendors = {VENDOR.vendor_id: VENDOR, OTHER_VENDOR.vendor_id: OTHER_VENDOR}
    s.fund_accounts["fa_TEST"] = FundAccount(
        "fa_TEST", dest_account, "KKBK0403467", "cont_1", VENDOR.vendor_id)
    return s


def event_body(payout_id="pout_TEST", fund_account_id="fa_TEST",
               amount=2800000, notes=None, created_at=None, event="payout.pending"):
    return {
        "id": f"evt_{payout_id}",
        "entity": "event",
        "event": event,
        "contains": ["payout"],
        "created_at": int(created_at if created_at is not None else time.time()),
        "payload": {"payout": {"entity": {
            "id": payout_id, "entity": "payout",
            "fund_account_id": fund_account_id,
            "amount": amount, "currency": "INR", "status": "pending",
            "notes": notes or {},
        }}},
    }


def sign(raw: bytes, secret=SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post(store, body=None, secret=SECRET, signature=None, raw=None):
    if raw is None:
        raw = json.dumps(body if body is not None else event_body()).encode()
    sig = sign(raw) if signature is None else signature
    return handle_payout_pending(raw, sig, store, secret=secret)


# ══ SECURITY ══════════════════════════════════════════════════════════

def test_valid_signature_is_accepted():
    r = post(make_store())
    assert r.status == 200


def test_forged_signature_is_rejected():
    r = post(make_store(), signature="0" * 64)
    assert r.status == 400
    assert r.outcome == webhook.HOLD


def test_missing_signature_is_rejected():
    r = post(make_store(), signature="")
    assert r.status == 400


def test_signature_from_the_wrong_secret_is_rejected():
    raw = json.dumps(event_body()).encode()
    r = handle_payout_pending(raw, sign(raw, "attacker_secret"), make_store(),
                              secret=SECRET)
    assert r.status == 400


def test_tampered_body_is_rejected():
    """Sign a legitimate body, then redirect the payout to another account."""
    original = json.dumps(event_body()).encode()
    good_sig = sign(original)
    tampered = json.loads(original)
    tampered["payload"]["payout"]["entity"]["fund_account_id"] = "fa_ATTACKER"
    r = handle_payout_pending(json.dumps(tampered).encode(), good_sig,
                              make_store(), secret=SECRET)
    assert r.status == 400


def test_signature_covers_raw_bytes_not_reserialised_json():
    """
    The classic bug. Re-serialising the parsed body changes key order and
    whitespace, so a handler that signs THAT would reject a legitimate event
    whose bytes differ only cosmetically. This asserts we verify the bytes as
    received: the same object with different spacing has a different signature
    and each must validate against its own.
    """
    body = event_body()
    compact = json.dumps(body, separators=(",", ":")).encode()
    spaced = json.dumps(body, indent=2).encode()
    assert compact != spaced
    assert verify_signature(compact, sign(compact), SECRET)
    assert verify_signature(spaced, sign(spaced), SECRET)
    # And crucially, a signature over one must NOT validate the other.
    assert not verify_signature(spaced, sign(compact), SECRET)


def test_verify_signature_never_falls_through_to_true():
    for raw, sig, sec in [
        (b"{}", "", SECRET), (b"{}", "abc", ""), (b"{}", None, SECRET),
        (None, "abc", SECRET), (b"{}", "abc", None),
    ]:
        assert verify_signature(raw, sig, sec) is False


def test_unconfigured_secret_refuses_to_process():
    """Never process unauthenticated events, even in a misconfigured deploy."""
    r = post(make_store(), secret="")
    assert r.status == 500
    assert r.outcome == webhook.HOLD


def test_replayed_old_event_is_rejected():
    """A captured event with a valid signature stays valid forever otherwise."""
    old = event_body(created_at=time.time() - webhook.MAX_EVENT_AGE_SECONDS - 60)
    r = post(make_store(), old)
    assert r.status == 400
    assert "replay window" in r.detail


def test_duplicate_event_is_not_reprocessed():
    store = make_store()
    raw = json.dumps(event_body()).encode()
    first = handle_payout_pending(raw, sign(raw), store, secret=SECRET)
    second = handle_payout_pending(raw, sign(raw), store, secret=SECRET)
    assert first.outcome != "DUPLICATE"
    assert second.outcome == "DUPLICATE"
    assert second.actions == []


def test_event_id_header_is_used_for_dedupe():
    """
    Razorpay's x-razorpay-event-id is what stays stable across retries of one
    delivery. The body id is only a fallback.
    """
    store = make_store()
    raw = json.dumps(event_body()).encode()
    a = handle_payout_pending(raw, sign(raw), store, secret=SECRET,
                              event_id_header="evt_from_header")
    b = handle_payout_pending(raw, sign(raw), store, secret=SECRET,
                              event_id_header="evt_from_header")
    assert a.outcome != "DUPLICATE"
    assert b.outcome == "DUPLICATE"


def test_retry_of_a_failed_delivery_is_still_processed():
    """
    Razorpay retries for up to 24 hours. A freshness window shorter than that
    turns one transient failure into a payout that is never decided at all —
    and a stuck payout is invisible, which is worse than the replay it guarded.
    """
    twelve_hours = time.time() - 12 * 3600
    r = post(make_store(), event_body(created_at=twelve_hours))
    assert r.status == 200
    assert r.outcome != webhook.HOLD or "replay" not in r.detail


def test_window_still_refuses_something_far_outside_any_retry():
    ancient = time.time() - webhook.MAX_EVENT_AGE_SECONDS - 3600
    r = post(make_store(), event_body(created_at=ancient))
    assert r.status == 400
    assert "replay window" in r.detail


def test_http_response_does_not_leak_the_audit_record():
    """
    The audit holds vendor identity, account numbers and the document reading.
    It belongs in server-side storage, not a response body.
    """
    from fastapi.testclient import TestClient
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
    client = TestClient(webhook.create_app(make_store()))
    raw = json.dumps(event_body()).encode()
    resp = client.post("/webhooks/razorpay", content=raw,
                       headers={"X-Razorpay-Signature": sign(raw)})
    body = resp.json()
    assert "audit" not in body
    # Not just the audit key: `detail` used to carry the rule's reason string,
    # which embeds the destination account number and the vendor's known
    # accounts. Assert on the whole serialised response, not one field.
    dump = json.dumps(body)
    assert KNOWN_ACCT not in dump
    assert VENDOR.legal_name not in dump
    assert VENDOR.known_phone not in dump

    # Rejection paths carry no audit, so a static non-identifying detail is
    # still returned and is useful for debugging a delivery.
    bad = client.post("/webhooks/razorpay", content=raw,
                      headers={"X-Razorpay-Signature": "0" * 64})
    assert bad.status_code == 400
    assert "signature" in bad.json()["detail"]


# ══ FAIL-SAFE: nothing may release a payout by accident ═══════════════

def test_unknown_fund_account_holds():
    r = post(make_store(), event_body(fund_account_id="fa_UNKNOWN"))
    assert r.outcome == webhook.HOLD
    assert r.actions == []


def test_fund_account_mapping_to_unknown_vendor_holds():
    store = make_store()
    store.fund_accounts["fa_ORPHAN"] = FundAccount(
        "fa_ORPHAN", NEW_ACCT, "X", "cont_9", "VEND_DOES_NOT_EXIST")
    r = post(store, event_body(fund_account_id="fa_ORPHAN"))
    assert r.outcome == webhook.HOLD


def test_malformed_body_holds():
    for raw in [b"not json", b"{}", b'{"event":"payout.processed"}',
                b'{"event":"payout.pending","payload":{}}']:
        r = handle_payout_pending(raw, sign(raw), make_store(), secret=SECRET)
        assert r.outcome == webhook.HOLD, raw
        assert r.audit is None


def test_no_path_releases_without_an_explicit_allow():
    """Sweep the failure inputs; none may produce payout_allowed."""
    cases = [
        dict(signature="0" * 64),
        dict(secret=""),
        dict(body=event_body(fund_account_id="fa_UNKNOWN")),
        dict(body=event_body(created_at=0)),
    ]
    for kw in cases:
        r = post(make_store(), **kw)
        assert not (r.audit or {}).get("payout_allowed"), kw


# ══ THE CORRELATION POLICY ════════════════════════════════════════════

def test_routine_payout_to_known_account_needs_no_document():
    """
    Most payouts have no change request, and holding all of them would make the
    control unusable. Nothing changed, so nothing needs authorising.
    """
    r = post(make_store(dest_account=KNOWN_ACCT))
    assert r.outcome == ALLOW
    assert r.audit["decision"]["rule_fired"] == "R2a_no_change_confirmed"
    assert r.audit["document"]["correlation"] == "none_found"
    assert r.audit["extraction"]["evidence_source"] == "no_document_supplied"


def test_new_destination_with_no_document_is_held():
    """
    The case this handler exists for: money is moving somewhere new and there
    is no authorisation for it anywhere.
    """
    r = post(make_store(dest_account=NEW_ACCT))
    assert r.outcome == STEP_UP
    assert r.audit["payout_allowed"] is False
    assert r.audit["decision"]["rule_fired"] == "R2b_followup_unverified_destination"


# ══ Triage as the front door (V2.2/V2.3 wired into the live path) ═════
# The inbox half and the payout half used to run without touching: decide()
# accepted inbox_signals and no caller in the live path supplied any. These
# assert the connection carries evidence, not merely that it does not crash.

def _inbox(*messages):
    from mcp import inbox_server as MCP
    return MCP.InboxServer("MERCH0001", list(messages))


def _stored(server=None):
    import triage as T
    s = make_store(dest_account=NEW_ACCT)
    s.inbox = server
    return s, T


def test_a_message_becomes_a_document_without_being_told_the_vendor():
    """
    /documents is handed a vendor_id. A mailbox is not. Triage resolving the
    sender with no model call is what makes the inbox a usable front door.
    """
    store, T = _stored()
    out = store.ingest_message(T.Message(
        message_id="<m1@x>", from_addr="accounts@balajilogistic.com",
        subject="INV-1", body=f"Everything reaches {NEW_ACCT} (KKBK0403467) now.",
        received_at=1000.0))
    assert out["verdict"] == T.ROUTE
    assert out["vendor_id"] == VENDOR.vendor_id
    assert store.documents[out["document_id"]]["vendor_id"] == VENDOR.vendor_id


def test_noise_never_becomes_a_document():
    """The funnel has to actually filter, or it is not a funnel."""
    store, T = _stored()
    out = store.ingest_message(T.Message(
        message_id="<m2@x>", from_addr="offers@quickfundsindia.co",
        subject="offer", body="Unlock working capital in 24 hours.",
        received_at=1000.0))
    assert out["document_id"] is None
    assert store.documents == {}


def test_inbox_evidence_reaches_the_decision_engine():
    """
    THE CONNECTION. A first contact from an unknown sender produces an inbox
    signal at ingest, and that signal has to survive into the payout decision
    days later — otherwise the whole inbox layer is decoration.
    """
    from mcp import inbox_server as MCP
    store, T = _stored(_inbox(
        MCP.StoredMessage("<old@x>", "T9", "accounts@balajilogistic.com",
                          "ap@clientcorp.in", "INV-0", "earlier mail", 10.0)))
    out = store.ingest_message(T.Message(
        message_id="<m3@x>", from_addr="ap@balajilogisticsgroupholdings.com",
        subject="INV-3",
        body=f"Our GST registration is {VENDOR.gstin}. "
             f"Everything reaches {NEW_ACCT} (KKBK0403467) now.",
        received_at=2000.0))
    assert out["match"] == "content"
    assert "inbox_first_contact" in out["inbox_signals"]

    doc = store.documents[out["document_id"]]
    assert [s.name for s in doc["inbox_signals"]] == out["inbox_signals"]

    # Stub the extractor: this test is about the WIRING, and letting it depend
    # on a live extraction would make it need an API key and go red whenever
    # another suite leaves a stub behind.
    import extractor as E
    real = E.extract
    E.extract = lambda *a, **k: ExtractionResult(
        ok=True, intent="BENEFICIARY_CHANGE", action="REPLACE_PAYOUT_DESTINATION",
        scope="OUTSTANDING_AND_FUTURE", proposed_account_number=NEW_ACCT,
        proposed_gstin=VENDOR.gstin, sender_domain=VENDOR.known_domain,
        amount=28000.0)
    try:
        r = post(store, event_body(
            notes={"payeeproof_document_id": out["document_id"]}))
    finally:
        E.extract = real
    names = [s["name"] for s in r.audit["decision"]["tier2"]]
    assert "inbox_first_contact" in names, names
    assert r.audit["payout_allowed"] is False


def test_inbox_evidence_is_gathered_at_arrival_not_at_payout_time():
    """
    Ordering, and it is not cosmetic. "Has this sender written before?" must be
    answered against the mailbox as it stood when the message arrived. Asking
    when the payout finally goes pending would count mail that landed in
    between and make a first contact look established.
    """
    from mcp import inbox_server as MCP
    store, T = _stored(_inbox())
    out = store.ingest_message(T.Message(
        message_id="<m4@x>", from_addr="accounts@balajilogistic.com",
        subject="INV-4", body=f"Everything reaches {NEW_ACCT} (KKBK0403467) now.",
        received_at=500.0))
    before = [s.name for s in store.documents[out["document_id"]]["inbox_signals"]]

    # Mail arrives afterwards. The stored evidence must not change.
    store.inbox._messages.append(
        MCP.StoredMessage("<later@x>", "T1", "accounts@balajilogistic.com",
                          "ap@clientcorp.in", "later", "later mail", 9000.0))
    after = [s.name for s in store.documents[out["document_id"]]["inbox_signals"]]
    assert before == after
    assert "inbox_first_contact" in before


def test_a_payout_decides_normally_with_no_inbox_connected():
    """
    Fail-safe on the whole layer. A deployment with no mailbox must behave
    exactly as it did before triage existed — inbox evidence corroborates and
    is never depended on.
    """
    store, T = _stored(server=None)
    out = store.ingest_message(T.Message(
        message_id="<m5@x>", from_addr="accounts@balajilogistic.com",
        subject="INV-5", body=f"Everything reaches {NEW_ACCT} (KKBK0403467) now.",
        received_at=1000.0))
    assert out["verdict"] == T.ROUTE
    assert out["inbox_signals"] == []
    r = post(store, event_body(notes={"payeeproof_document_id": out["document_id"]}))
    assert r.audit["payout_allowed"] is False
    assert r.audit["decision"]["rule_fired"]
    assert r.audit["decision"]["tier2"] is not None


def test_the_same_message_is_not_triaged_twice():
    store, T = _stored()
    m = dict(message_id="<dup@x>", from_addr="accounts@balajilogistic.com",
             subject="INV-6", body=f"Pay {NEW_ACCT} KKBK0403467.", received_at=1.0)
    first = store.ingest_message(T.Message(**m))
    second = store.ingest_message(T.Message(**m))
    assert first["verdict"] == T.ROUTE
    assert second["verdict"] == T.DUPLICATE


def test_destination_on_another_vendor_is_held_and_recommended_for_rejection():
    r = post(make_store(dest_account=OTHER_ACCT))
    assert r.outcome == STEP_UP
    assert r.audit["payout_allowed"] is False
    assert r.audit["decision"]["rule_fired"] == "R2c_followup_destination_conflict"
    assert r.audit["decision"]["recommended_action"] == "reject"
    # And it reaches the action plan, flagged, so nothing can act on it alone.
    assert all(a["requires_human_confirmation"]
               for a in r.audit["razorpay_actions"])


def test_destination_comes_from_the_payout_not_the_document():
    """
    P0.3, enforced at the integration boundary. The document names the vendor's
    genuine account while the payout points elsewhere; the payout wins.
    """
    store = make_store(dest_account=NEW_ACCT)
    store.put_document("doc_1", VENDOR.vendor_id,
                       f"Please pay to our usual account {KNOWN_ACCT}.")
    r = post(store, event_body(notes={"payeeproof_document_id": "doc_1"}))
    assert r.audit["destination"]["account_number"] == NEW_ACCT
    assert r.audit["destination"]["source"] == "razorpay_fund_account"
    assert r.audit["payout_allowed"] is False


def test_explicit_note_correlates_the_document():
    store = make_store(dest_account=KNOWN_ACCT)
    store.put_document("doc_9", VENDOR.vendor_id, "Chasing INV-4471, no changes.")
    r = post(store, event_body(notes={"payeeproof_document_id": "doc_9"}))
    assert r.audit["document"]["document_id"] == "doc_9"
    assert r.audit["document"]["correlation"] == "explicit_note"


def test_document_belonging_to_another_vendor_is_not_used():
    """
    A mis-pointed reference must not silently fall back to a different
    document — guessing is worse than holding.
    """
    store = make_store(dest_account=KNOWN_ACCT)
    store.put_document("doc_x", OTHER_VENDOR.vendor_id, "unrelated")
    r = post(store, event_body(notes={"payeeproof_document_id": "doc_x"}))
    assert r.audit["document"]["document_id"] is None
    assert r.audit["document"]["correlation"] == "none_found"


def test_stale_document_outside_the_window_is_ignored():
    store = make_store(dest_account=KNOWN_ACCT)
    store.put_document("doc_old", VENDOR.vendor_id, "ancient",
                       received_at=time.time() - (webhook.CORRELATION_WINDOW_DAYS + 5) * 86400)
    assert store.find_document(VENDOR.vendor_id) is None


def test_most_recent_document_wins():
    store = make_store()
    store.put_document("older", VENDOR.vendor_id, "a", received_at=time.time() - 5000)
    store.put_document("newer", VENDOR.vendor_id, "b", received_at=time.time() - 10)
    assert store.find_document(VENDOR.vendor_id)["document_id"] == "newer"


# ══ Details that are quietly load-bearing ═════════════════════════════

def test_amount_is_converted_from_paise():
    """Razorpay sends paise. Rupees would be compared 100x too high."""
    evt = parse_payout_pending(event_body(amount=2800000))
    assert evt.amount_rupees == 28000.0


def test_no_document_evidence_is_marked_as_synthetic():
    ext = no_document_evidence()
    assert ext.ok is True
    assert ext.evidence_source == "no_document_supplied"
    assert ext.raw_llm_output is None


def test_allow_emits_approve_and_block_emits_reject_plus_deactivate():
    allow = post(make_store(dest_account=KNOWN_ACCT))
    assert [a["method"] for a in allow.actions] == ["POST"]
    assert "approve" in allow.actions[0]["endpoint"]

    block = post(make_store(dest_account=OTHER_ACCT))
    assert [a["method"] for a in block.actions] == ["POST", "PATCH"]
    assert "reject" in block.actions[0]["endpoint"]
    assert block.actions[1]["body"] == {"active": False}


def test_held_payout_emits_no_api_call():
    r = post(make_store(dest_account=NEW_ACCT))
    assert all(a["method"] is None for a in r.actions)


# ══ Operator dashboard ════════════════════════════════════════════════
# It deliberately shows what the webhook response withholds. These assert both
# halves of that: the dashboard shows the evidence, and the response still does
# not.

def _client(store):
    from fastapi.testclient import TestClient
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
    return TestClient(webhook.create_app(store))


def _fire(client, store, dest, payout_id):
    raw = json.dumps(event_body(payout_id=payout_id)).encode()
    handle_payout_pending(raw, sign(raw), store, secret=SECRET)


def test_dashboard_is_empty_before_any_events():
    r = _client(make_store()).get("/")
    assert r.status_code == 200
    assert "No payout.pending" in r.text


def test_dashboard_lists_a_decision():
    store = make_store(dest_account=OTHER_ACCT)
    client = _client(store)
    _fire(client, store, OTHER_ACCT, "pout_1")
    body = client.get("/").text
    assert "pout_1" in body
    assert "R2c_followup_destination_conflict" in body
    # The hold and the recommendation both: without the second, a BEC case and
    # a routine unfamiliar-account hold are the same pill in the queue.
    assert "STEP_UP_VERIFY" in body
    assert "RECOMMEND REJECT" in body


def test_the_case_view_names_the_account_to_demand_the_penny_drop_from():
    """
    THE CONTROL, and it was computed and rendered nowhere.

    "Prove you control an account on file" lets an attacker use one they
    planted months ago. Naming the account is the entire defence — so an
    operator who cannot see WHICH account cannot perform the check that makes
    the system work. verification_account sat in the audit record, unrendered.
    """
    import dataclasses, verifier, dashboard
    from decision_engine import Decision, AccountRecord, STEP_UP
    # The shared VENDOR fixture carries no accounts, so build one that can
    # actually qualify: settled, seasoned, and added at onboarding.
    seasoned = dataclasses.replace(VENDOR, accounts=[AccountRecord(
        account_number=KNOWN_ACCT, ifsc="KKBK0403467",
        added_on="2023-02-14", added_via="onboarding",
        verified_by="onboarding_kyc", settled_payout_count=11,
        is_primary=True)])
    d = Decision(outcome=STEP_UP, rule_fired="R5", reason="held",
                 triggered_by=[], needs_callback=True)
    ver = verifier.verify(d, seasoned, False, "C1",
                          requester_controls_accounts=[],
                          as_of="2026-06-30")
    assert ver.verification_account, "the verifier should have named one"

    html = dashboard.render_case({
        "payout_id": "pout_x", "decision": d.to_dict(),
        "verification": ver.to_dict(), "final_outcome": STEP_UP,
        "razorpay_actions": [],
    })
    assert ver.verification_account in html, "named account not shown"
    assert "Rs 1 from" in html
    assert ver.verification_account_basis[:24] in html, "no justification shown"


def test_the_case_view_distinguishes_unavailable_from_failed():
    """
    Channel 2 unavailable is a THIRD state, not a quiet failure. An operator who
    reads it as "the check failed" draws the opposite conclusion from the one
    the evidence supports.
    """
    import dataclasses, verifier, dashboard
    from decision_engine import Decision, AccountRecord, STEP_UP
    bare = dataclasses.replace(VENDOR, accounts=[
        AccountRecord(account_number="999900001111", settled_payout_count=0,
                      is_primary=True)])
    d = Decision(outcome=STEP_UP, rule_fired="R5", reason="held",
                 triggered_by=[], needs_callback=True)
    ver = verifier.verify(d, bare, True, "C1",
                          requester_controls_accounts=[], as_of="2026-06-30")
    assert ver.outcome == verifier.UNAVAILABLE_C2
    html = dashboard.render_case({
        "payout_id": "pout_y", "decision": d.to_dict(),
        "verification": ver.to_dict(), "final_outcome": STEP_UP,
        "razorpay_actions": [],
    })
    assert "No account qualifies" in html
    assert "never falls back" in html


def test_the_case_view_states_the_two_person_rule():
    """
    A single Approve button quietly removes segregation of duties. Stating the
    requirement is the cheapest way to keep it visible while the workflow that
    enforces it does not exist yet.
    """
    import dashboard
    from decision_engine import Decision, STEP_UP
    d = Decision(outcome=STEP_UP, rule_fired="R5", reason="held",
                 triggered_by=[], needs_callback=True)
    html = dashboard.render_case({
        "payout_id": "p", "decision": d.to_dict(), "final_outcome": STEP_UP,
        "verification": {"outcome": "UNREACHABLE", "contact_used": "9",
                         "reason": "no answer"},
        "razorpay_actions": [],
    })
    assert "Two people, not one" in html


def test_the_inbox_view_shows_what_triage_dropped():
    """
    A funnel that displays only its successes is not showing its work. The
    dropped messages are how anyone audits that it is not quietly discarding
    real change requests — the failure this layer is most exposed to.
    """
    import triage as T
    store = make_store(dest_account=NEW_ACCT)
    store.ingest_message(T.Message(
        message_id="<keep@x>", from_addr="accounts@balajilogistic.com",
        subject="INV-1", body=f"Everything reaches {NEW_ACCT} (KKBK0403467).",
        received_at=10.0))
    store.ingest_message(T.Message(
        message_id="<drop@x>", from_addr="offers@quickfundsindia.co",
        subject="offer", body="Unlock working capital in 24 hours.",
        received_at=20.0))
    assert len(store.triage_log) == 2

    import dashboard
    html = dashboard.render_inbox(list(store.triage_log))
    assert "Needs review" in html and "Filtered out" in html
    assert "quickfundsindia.co" in html, "a dropped message is not shown"
    assert "balajilogistic.com" in html

    # And every message is openable, the dropped one included. A queue that
    # only opens its own hits cannot be audited by anyone who does not already
    # trust it, which is exactly the reviewer this section exists for.
    from urllib.parse import quote
    for mid in ("<drop@x>", "<keep@x>"):
        href = "/message/" + quote(mid, safe="")
        assert href in html, f"{mid} cannot be opened"


def test_any_message_can_be_opened_and_shows_what_the_sender_wrote():
    """
    The operator has to be able to read the email, not just the verdict.

    A screen that shows a rule name and a recommendation asks a human to
    rubber-stamp a machine. The whole design puts the decision back on a person,
    and a person cannot exercise that unless the evidence they are judging — the
    supplier's own words — is on the page. Dropped messages open too, because
    "triage binned a real request" is only auditable by reading what it binned.
    """
    import triage as T
    from urllib.parse import quote
    store = make_store(dest_account=NEW_ACCT)
    store.ingest_message(T.Message(
        message_id="<open@x>", from_addr="accounts@balajilogistic.com",
        subject="Updated bank details for INV-88",
        body=f"Please send all future payments to {NEW_ACCT} (KKBK0403467).",
        received_at=10.0))
    store.ingest_message(T.Message(
        message_id="<binned@x>", from_addr="offers@quickfundsindia.co",
        subject="offer", body="Unlock working capital in 24 hours.",
        received_at=20.0))
    client = _client(store)

    r = client.get("/message/" + quote("<open@x>", safe=""))
    assert r.status_code == 200
    assert NEW_ACCT in r.text, "the message body is not on the page"
    assert "Updated bank details" in r.text

    r = client.get("/message/" + quote("<binned@x>", safe=""))
    assert r.status_code == 200
    assert "working capital" in r.text, "a filtered message cannot be read"

    assert client.get("/message/" + quote("<nosuch@x>", safe="")).status_code == 200


def test_the_queue_speaks_english_not_identifiers():
    """
    `R5_tier1_inconclusive` is a variable name. An accounts-payable clerk has to
    act on it, and cannot act on a symbol they have to be taught. The identifier
    stays on the page for the auditor — the rule table is the authoritative
    policy and a paraphrase is not — but it is not the headline.
    """
    import dashboard
    a = {"payout_id": "pout_x", "final_outcome": "STEP_UP_VERIFY",
         "decision": {"rule_fired": "R5_tier1_inconclusive",
                      "reason": "identity unconfirmed"}}
    html = dashboard.render_message({
        "message_id": "<v@x>", "from": "a@b.com", "subject": "s",
        "body": "b", "verdict": "ROUTE", "match": "exact",
        "final_outcome": "STEP_UP_VERIFY", "audit": a,
        "rule_fired": "R5_tier1_inconclusive"})
    assert "Identity checks could not be completed" in html
    assert "On hold" in html
    assert "R5_tier1_inconclusive" in html, "the auditor lost the rule name"


def test_the_inbox_view_flags_how_the_vendor_was_matched():
    """
    "content" means the sender matched nothing and was resolved only from an
    identifier in the body — which anyone can type. A reviewer needs that on
    the row, not buried in a case page.
    """
    import triage as T, dashboard
    store = make_store(dest_account=NEW_ACCT)
    store.ingest_message(T.Message(
        message_id="<c@x>", from_addr="ap@some-unrelated-parent.example",
        subject="INV-9",
        body=f"Our GST registration is {VENDOR.gstin}. "
             f"Everything reaches {NEW_ACCT} (KKBK0403467).",
        received_at=30.0))
    row = list(store.triage_log)[0]
    assert row["match"] == "content"
    html = dashboard.render_inbox([row])
    assert "matched only on an identifier in the body" in html


def test_case_view_shows_the_signal_table():
    """
    The point of the dashboard: a held payout has to be explainable to the
    vendor whose money it is.
    """
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_2")
    body = client.get("/case/pout_2").text
    assert "account_continuity" in body
    assert NEW_ACCT in body
    assert "razorpay_fund_account" in body


def test_the_operator_can_record_a_callback_and_the_case_moves():
    """
    The buttons record facts established outside this system. Nothing about a
    phone call can be observed by the engine, so the only honest thing the
    screen can do is take the operator's word for it and record who gave it.
    """
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_call")

    r = client.post("/case/pout_call/action",
                    data={"action": "callback_confirmed",
                          "actor": "Priya Menon",
                          "note": "Spoke to Balaji on the number we hold."})
    assert r.status_code == 200
    assert "Refused" not in r.text, r.text[:400]
    assert casefile.state_of(store.case("pout_call")) == "verified"
    assert "Priya Menon" in r.text
    assert "Spoke to Balaji" in r.text, "the note is not on the case file"


def test_the_verifier_cannot_release_over_http_either():
    """
    The two-person rule is enforced on the server, so it cannot be skipped by
    posting the form directly. A control that only exists as a disabled button
    is not a control.
    """
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_sod")

    client.post("/case/pout_sod/action",
                data={"action": "callback_confirmed", "actor": "Priya Menon"})
    r = client.post("/case/pout_sod/action",
                    data={"action": "released", "actor": "Priya Menon"})
    assert "Refused" in r.text, "the verifier released their own case"
    assert casefile.state_of(store.case("pout_sod")) == "verified"

    r = client.post("/case/pout_sod/action",
                    data={"action": "released", "actor": "Rahul Iyer"})
    assert "Refused" not in r.text
    assert casefile.state_of(store.case("pout_sod")) == "released"


def test_nothing_releases_over_http_without_a_verification():
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_bare")
    r = client.post("/case/pout_bare/action",
                    data={"action": "released", "actor": "Rahul Iyer"})
    assert "Refused" in r.text
    assert casefile.state_of(store.case("pout_bare")) == "open"


def test_an_invented_action_is_refused_not_recorded():
    """
    The form is attacker-reachable in exactly the way every other endpoint is.
    A free-text action would let anyone write anything into the audit trail.
    """
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_bogus")
    r = client.post("/case/pout_bogus/action",
                    data={"action": "release_everything", "actor": "Priya Menon"})
    assert "Refused" in r.text
    assert store.case("pout_bogus") == []


def test_a_closed_case_accepts_nothing_further():
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_closed")
    client.post("/case/pout_closed/action",
                data={"action": "rejected", "actor": "Meera Nair"})
    before = len(store.case("pout_closed"))
    r = client.post("/case/pout_closed/action",
                    data={"action": "callback_confirmed", "actor": "Priya Menon"})
    assert "Refused" in r.text
    assert len(store.case("pout_closed")) == before


def test_the_case_page_offers_the_penny_drop_against_the_named_account():
    """
    The account to demand was already computed and shown. The button has to
    carry that same account, or an operator can record "rupee received" without
    the record saying from where — which is the whole control.
    """
    store = make_store(dest_account=NEW_ACCT)
    client = _client(store)
    _fire(client, store, NEW_ACCT, "pout_pd")
    body = client.get("/case/pout_pd").text
    a = store.find_audit("pout_pd")
    named = (a.get("verification_demand") or {}).get("account_number")
    if named:
        assert 'name="detail"' in body
        assert named in body


def test_the_case_shows_how_each_account_got_onto_the_file():
    """
    The engine holds a payout because an account is "on file but never
    established". That sentence is unauditable unless the operator can see the
    four facts it rests on — when the account was added, how, what verified it,
    and whether it has ever carried money.

    Deliberately scoped to this vendor's accounts rather than a browsable
    vendor master: the master is what a planted-account attack corrupts, and a
    screen inviting someone to eyeball it and conclude the request looks fine
    is the reasoning that attack defeats.
    """
    from decision_engine import AccountRecord, VendorRecord
    import dataclasses
    store = make_store(dest_account=KNOWN_ACCT)
    # The fixture vendor deliberately carries no AccountRecords — most tests
    # here predate them. This one needs a master that records provenance,
    # because a master that does not is exactly the case the panel must not
    # invent an answer for.
    store.vendors[VENDOR.vendor_id] = dataclasses.replace(
        VENDOR, accounts=[
            AccountRecord(account_number=KNOWN_ACCT, ifsc="KKBK0403467",
                          is_primary=True, added_on="2022-10-16",
                          added_via="onboarding", verified_by="onboarding_kyc",
                          settled_payout_count=39),
            AccountRecord(account_number=NEW_ACCT, ifsc="SBIN0000002",
                          added_on="2026-05-01", added_via="email_request",
                          verified_by="unverified", settled_payout_count=0),
        ])
    client = _client(store)
    _fire(client, store, KNOWN_ACCT, "pout_prov")
    a = store.find_audit("pout_prov")
    rows = a.get("accounts_on_file")
    assert rows, "the audit carries no account provenance"
    dest = [r for r in rows if r["is_destination"]]
    assert len(dest) == 1, "the destination is not identified among the accounts"

    body = client.get("/case/pout_prov").text
    assert "Accounts on file" in body
    for r in rows:
        assert r["account_number"] in body, r["account_number"]
    assert "This payment" in body


def test_the_panel_says_when_the_destination_is_none_of_them():
    """
    The commonest hold. Listing the known accounts without saying the money is
    going somewhere else reads as though nothing is wrong.
    """
    import dashboard
    html = dashboard.render_case({
        "payout_id": "p", "final_outcome": "STEP_UP_VERIFY",
        "decision": {"rule_fired": "R5_tier1_inconclusive", "reason": "held"},
        "destination": {"account_number": "555000111", "source": "razorpay_payout"},
        "accounts_on_file": [
            {"account_number": "111", "ifsc": "H", "status": "active",
             "is_primary": True, "is_destination": False, "added_on": "2022-01-01",
             "added_via": "onboarding", "verified_by": "onboarding_kyc",
             "settled_payout_count": 12, "established": True}]})
    assert "not one of the accounts above" in html
    assert "555000111" in html


def test_an_unestablished_account_is_marked_as_such_on_the_page():
    """
    The whole point of the panel. An account that reached the file because one
    email asked for it, with nothing outside email confirming it and no payout
    ever settled, must not look identical to one that has been paid for years.
    """
    import dashboard
    a = {"payout_id": "pout_x", "final_outcome": "STEP_UP_VERIFY",
         "decision": {"rule_fired": "R5_tier1_inconclusive", "reason": "held"},
         "destination": {"account_number": "999", "source": "razorpay_payout"},
         "accounts_on_file": [
             {"account_number": "111", "ifsc": "HDFC0000001", "status": "active",
              "is_primary": True, "is_destination": False,
              "added_on": "2022-10-16", "added_via": "onboarding",
              "verified_by": "onboarding_kyc", "settled_payout_count": 39,
              "established": True},
             {"account_number": "999", "ifsc": "SBIN0000002", "status": "active",
              "is_primary": False, "is_destination": True,
              "added_on": "2026-05-01", "added_via": "email_request",
              "verified_by": "unverified", "settled_payout_count": 0,
              "established": False},
         ]}
    html = dashboard.render_case(a)
    assert "Never established" in html
    assert "Added because of an email request" in html
    assert "Verified at onboarding (KYC)" in html
    assert "Never verified outside email" in html
    # And the identifiers stay out of the operator's way.
    assert ">email_request<" not in html
    assert ">onboarding_kyc<" not in html


def test_a_vendor_with_no_recorded_provenance_shows_no_panel():
    """
    Silence is not suspicion. A merchant whose master lacks these columns gets
    no panel rather than a table of blanks implying everything is unverified.
    """
    import dashboard
    html = dashboard.render_case(
        {"payout_id": "p", "final_outcome": "ALLOW",
         "decision": {"rule_fired": "R7_all_clear", "reason": "clean"}})
    assert "Accounts on file" not in html


def test_unknown_case_does_not_error():
    assert _client(make_store()).get("/case/pout_nope").status_code == 200


def test_dashboard_escapes_vendor_controlled_text():
    """
    Vendor names, domains and the model's own output are rendered into HTML.
    None of it is trusted input.
    """
    hostile = VendorRecord(
        vendor_id="<script>alert(1)</script>", legal_name="x", gstin="g",
        known_domain="d.com", known_phone="9", known_account_number=KNOWN_ACCT,
        known_ifsc="I", avg_payout_amount=1.0)
    store = make_store()
    store.vendors[hostile.vendor_id] = hostile
    store.fund_accounts["fa_TEST"] = FundAccount(
        "fa_TEST", KNOWN_ACCT, "I", "c", hostile.vendor_id)
    client = _client(store)
    _fire(client, store, KNOWN_ACCT, "pout_3")
    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_recording_a_decision_did_not_loosen_the_response():
    """
    Regression. Retaining audits for the dashboard must not put them back into
    the HTTP reply, where `detail` previously leaked the account number too.
    """
    store = make_store()
    client = _client(store)
    raw = json.dumps(event_body()).encode()
    resp = client.post("/webhooks/razorpay", content=raw,
                       headers={"X-Razorpay-Signature": sign(raw)})
    dump = json.dumps(resp.json())
    assert "audit" not in resp.json()
    assert KNOWN_ACCT not in dump
    assert VENDOR.legal_name not in dump
    # ...while the dashboard, a different audience, does show it.
    assert KNOWN_ACCT in client.get("/").text


def test_audit_history_is_bounded():
    """Unbounded in-memory retention is a slow leak on a long-running process."""
    store = Store(audit_limit=5)
    for i in range(12):
        store.record_audit({"payout_id": f"p{i}"})
    assert len(store.audits) == 5
    assert store.audits[0]["payout_id"] == "p11"   # newest first


# ══ Runner ════════════════════════════════════════════════════════════

def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n        {e or 'assertion failed'}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print()
    print(f"  {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
