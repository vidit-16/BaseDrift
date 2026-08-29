"""
PayeeProof — webhook demo.

Drives the real FastAPI app over real HTTP with real HMAC-signed bodies. Nothing
here is stubbed except the RazorpayX side, which is a local store rather than a
live account: FAV and Approval Workflow are both unavailable in test mode, and
this file never pretends otherwise.

    python src/webhook_demo.py

Four of the five scenarios need no API key. The fifth reads an actual
change-request email and needs an API key; it is skipped with a note if unset.

To run the server for yourself instead:
    set RAZORPAY_WEBHOOK_SECRET=whsec_demo
    uvicorn webhook_app:app --app-dir src --port 8000
"""

import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_client  # noqa: E402
from decision_engine import VendorRecord  # noqa: E402
from webhook import FundAccount, Store, create_app  # noqa: E402

SECRET = "whsec_demo_not_a_real_secret"

VENDOR = VendorRecord(
    vendor_id="VEND0069", legal_name="Balaji Logistics",
    gstin="07JQQPG8009O1Z2", known_domain="balajilogistic.com",
    known_phone="9088190947", known_account_number="434392416664",
    known_ifsc="KKBK0403467", avg_payout_amount=28000.0,
)
OTHER = VendorRecord(
    vendor_id="VEND0123", legal_name="Nova Packaging",
    gstin="27AAAAA0000A1Z5", known_domain="novapackaging.com",
    known_phone="9000000000", known_account_number="999988887777",
    known_ifsc="HDFC0001234", avg_payout_amount=45000.0,
)

CHANGE_REQUEST = """
From: payments@balaj1logistic.com
To: accounts@clientcorp.in

Hi Meera,

Our treasury has consolidated everything into a single facility this quarter.
INV-4471 from October is still open on our ledger, and the retainer continues
through March. Everything should reach 351349409853, KKBK0238196 from here.

Our GST registration should be the same as before, 07JQQPG8009O1Z2.

Month-end closing is tomorrow, so do prioritise this one. Please reply on this
thread rather than the old chain.

Priya Nair
Accounts, Balaji Logistics
""".strip()


def build_store():
    store = Store()
    store.vendors = {VENDOR.vendor_id: VENDOR, OTHER.vendor_id: OTHER}
    store.fund_accounts = {
        # The account this vendor has always been paid at.
        "fa_usual": FundAccount("fa_usual", "434392416664", "KKBK0403467",
                                "cont_bal", VENDOR.vendor_id),
        # A fund account created from the spoofed email above.
        "fa_new": FundAccount("fa_new", "351349409853", "KKBK0238196",
                              "cont_bal", VENDOR.vendor_id),
        # A mule: an account already on file under a DIFFERENT vendor.
        "fa_mule": FundAccount("fa_mule", "999988887777", "HDFC0001234",
                               "cont_bal", VENDOR.vendor_id),
    }
    return store


def signed_post(client, fund_account_id, notes=None, secret=SECRET, label=""):
    body = {
        "id": f"evt_{fund_account_id}_{int(time.time()*1000)}",
        "entity": "event", "event": "payout.pending", "contains": ["payout"],
        "created_at": int(time.time()),
        "payload": {"payout": {"entity": {
            "id": f"pout_{fund_account_id}", "entity": "payout",
            "fund_account_id": fund_account_id, "amount": 2800000,
            "currency": "INR", "status": "pending", "notes": notes or {},
        }}},
    }
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    resp = client.post("/webhooks/razorpay", content=raw, headers={
        "X-Razorpay-Signature": sig, "Content-Type": "application/json",
        "x-razorpay-event-id": body["id"]})
    # The HTTP response deliberately carries no audit record — it holds vendor
    # identity and account numbers, which belong in server-side storage. The
    # demo re-runs the handler directly purely to display what was recorded.
    audit = None
    if resp.status_code == 200:
        from webhook import handle_payout_pending
        replay = handle_payout_pending(
            raw, sig, client.app.state.store, secret=secret,
            event_id_header=body["id"] + "_demo_display")
        audit = replay.audit
    return resp, audit


def show(label, expectation, sent):
    resp, audit = sent
    j = resp.json()
    print(f"  {label}")
    print(f"    expected : {expectation}")
    print(f"    HTTP {resp.status_code}   outcome: {j['outcome']}")
    a = audit
    if not a:
        print(f"    detail   : {j['detail']}")
        print()
        return
    d = a["destination"]
    print(f"    destination : {d['account_number']}  (from the {d['source']})")
    print(f"    document    : {a['document']['document_id'] or 'none'} "
          f"({a['document']['correlation']})")
    print(f"    evidence    : {a['extraction']['evidence_source']}")
    print(f"    rule fired  : {a['decision']['rule_fired']}")
    print(f"    payout_allowed = {a['payout_allowed']}")
    for act in a["razorpay_actions"]:
        if act["method"]:
            flag = ('   [RECOMMENDED — needs human confirmation]'
                    if act.get('requires_human_confirmation') else '')
            print(f"    api         : {act['method']} {act['endpoint']}{flag}")
        else:
            print(f"    api         : {act['effect']}")
    print()


def main():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise SystemExit("pip install -r requirements.txt")

    os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
    store = build_store()
    client = TestClient(create_app(store))

    print()
    print("=" * 74)
    print("PayeeProof — payout.pending webhook")
    print("Every payout below is frozen. No money has moved yet.")
    print("=" * 74)
    print()

    print("1. ROUTINE PAYOUT — no change was requested, destination unchanged")
    show("payout -> the account this vendor has always used",
         "ALLOW. Nothing changed, so nothing needs authorising.",
         signed_post(client, "fa_usual"))

    print("2. REDIRECTED PAYOUT — new destination, nothing authorising it")
    show("payout -> an account never seen for this vendor, no document",
         "HELD. Money moving somewhere new with no change request on file.",
         signed_post(client, "fa_new"))

    print("3. MULE ACCOUNT — destination belongs to a different vendor")
    show("payout -> an account already on file under another contact",
         ("HOLD + recommend reject. Cross-contact reuse is how one attacker "
          "collects from many — and since V2.1 even that ends with a human."),
         signed_post(client, "fa_mule"))

    print("4. FORGED WEBHOOK — an attacker calling the endpoint directly")
    show("valid-looking body, signature signed with the wrong secret",
         "400. Refused before the body is parsed or trusted.",
         signed_post(client, "fa_usual", secret="attacker_guessed_this"))

    print("5. THE BEC CASE — a real change-request email, read by the model")
    if not llm_client.get_api_key():
        print("    SKIPPED — no API key is set, so the semantic layer")
        print("    cannot run. The demo will not fabricate a verdict without it.")
        print()
    else:
        r = client.post("/documents", json={
            "document_id": "doc_bec", "vendor_id": VENDOR.vendor_id,
            "text": CHANGE_REQUEST})
        print(f"    AP system posted the change request -> {r.json()['document_id']}")
        show("payout -> the account named in that email",
             ("HOLD + recommend reject. Every bank-level check passes; "
              "authorisation is absent."),
             signed_post(client, "fa_new",
                         notes={"payeeproof_document_id": "doc_bec"}))

    print("=" * 74)
    print("The safe state is inaction: a pending payout stays pending unless")
    print("something explicitly approves it. Every refusal above left the money")
    print("exactly where it was.")
    print("=" * 74)
    print()


if __name__ == "__main__":
    main()
