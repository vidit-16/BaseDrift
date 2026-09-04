"""
PayeeProof — the two-minute demo.

    python src/demo.py
    python src/demo.py --serve      # then open http://localhost:8000/

One real fraud case from the evaluation corpus, carried end to end through the
real code: triage, the MCP inbox, the extractor, the rule engine, the two
verification channels, and the operator's view. Nothing here is mocked for
effect — the webhook request is genuinely signed and genuinely verified, and
every decision comes from decision_engine.decide().

Runs with or without an API key. With one, the extractor reads the email live.
Without, it loads the cached reading from the evaluation run and says so. It
never invents a verdict.

Contrast with src/webhook_demo.py, which is a technical smoke test of five
scenarios. This file tells one story.
"""

import csv
import hashlib
import hmac
import json
import json as _json
import os
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "data"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import llm_client  # noqa: E402
import pipeline  # noqa: E402
import extractor as E  # noqa: E402
import triage as T  # noqa: E402
import verifier  # noqa: E402
import webhook as W  # noqa: E402
from mcp import inbox_server as MCP  # noqa: E402

DATA = os.path.join(ROOT, "data")
SECRET = "whsec_demo"

# One day's inbound at the merchant's stated volume, rather than a fixed 220.
# When the mailbox was 7.7% change requests, 220 messages held plenty to look
# at; at a realistic 2.4% the same slice holds about one fraudulent request,
# which makes the queue honest and the demo empty. A day's worth keeps both.
#
# A module constant because tests/test_webhook.py builds the same store to
# check the replayed queue, and the two silently drifted apart the first time
# this number moved.
INBOX_MESSAGES = 500
CASE_ID = "CASE00593"
W_COL = 74


def rule(ch="─"):
    print(ch * W_COL)


def scene(n, title):
    print()
    rule("━")
    print(f"  {n}   {title}")
    rule("━")


def beat(text=""):
    print(f"      {text}" if text else "")


def block(text, indent=8):
    """Pre-formatted output: indent it, never re-wrap it."""
    pad = " " * indent
    for line in text.split("\n"):
        print(f"{pad}{line}" if line else "")


def quote(text, indent=8):
    """
    The vendor's own words, wrapped.

    A demo is usually read on a projector or a shared screen. The corpus emails
    run past 180 characters on one line, and the part a viewer most wants to
    read was the part running off the right edge.
    """
    pad = " " * indent
    for para in text.split("\n"):
        if not para.strip():
            print()
            continue
        for line in textwrap.wrap(para.strip(), width=W_COL - indent - 2):
            print(f"{pad}{line}")


def use_cached_extraction_if_no_key(case_id):
    """
    With no API key the extractor fails, R1 fires, and the payout is held — a
    hold that LOOKS like a catch and is really an outage. README says exactly
    this about pipeline.py, which refuses to run rather than take the credit.

    A demo cannot refuse to run, so it loads the reading recorded during the
    evaluation and labels it. Returns the label to print, or None if a key is
    present and the extractor will read the message live.
    """
    if llm_client.get_api_key():
        return None

    import json as _json
    import extraction_eval as X
    import render as R
    import extractor as E

    rendered = next(c for c in R.render_split("dev") if c.case_id == case_id)
    path = X.cache_path(rendered.sha256, "openai/gpt-oss-120b", 1)
    if not os.path.exists(path):
        return "NO CACHE — set PAYEEPROOF_API_KEY to read the message"

    with open(path, encoding="utf-8") as f:
        payload = _json.load(f)
    meta = payload.get("_meta", {})
    E.extract = lambda *a, **k: X.result_from_dict(payload)
    return (f"cached from the evaluation run "
            f"({meta.get('model', '?')}, {meta.get('extracted_at', '?')[:10]})")


def dashboard_preview(store):
    """
    The real dashboard HTML, reduced to the rows a viewer would read.

    Printing a URL and hoping uvicorn is already running is a dead end in the
    middle of a demo. This renders the same function the server does, so what
    appears here cannot drift from what the browser shows.
    """
    import html as _html
    import re
    import dashboard

    page = dashboard.render_index(store.recent_audits())

    def cells(row):
        out = []
        for td in re.findall(r"<td.*?>(.*?)</td>", row, re.S):
            txt = re.sub(r"<[^>]+>", " ", td)
            out.append(re.sub(r"\s+", " ", _html.unescape(txt)).strip())
        return out

    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not body:
        return "no decisions recorded yet"

    sub = re.search(r'<div class="sub">(.*?)</div>', page, re.S)
    lines = []
    if sub:
        lines.append(_html.unescape(re.sub(r"<[^>]+>", "", sub.group(1))).strip())
        lines.append("")
    lines.append(f"{'payout':13s} {'vendor':9s} {'rule fired':36s} outcome")
    for row in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S):
        c = cells(row)
        if len(c) < 6:
            continue
        payout, vendor, _dest, _doc, fired, outcome = c[:6]
        lines.append(f"{payout:13s} {vendor:9s} {fired:36s} {outcome}")
    return "\n".join(lines)


def load_case():
    with open(os.path.join(DATA, "cases_dev.csv"), newline="", encoding="utf-8") as f:
        case = next(r for r in csv.DictReader(f) if r["case_id"] == CASE_ID)
    with open(os.path.join(DATA, "inbox_dev.csv"), newline="", encoding="utf-8") as f:
        msg = next(r for r in csv.DictReader(f) if r["case_id"] == CASE_ID)
    return case, msg


def build_store(vendors, case):
    inbox = MCP.from_csv("dev")
    store = W.Store(inbox=inbox)
    store.vendors = vendors
    v = vendors[case["vendor_id"]]
    # Two fund accounts: where this vendor has always been paid, and where the
    # email wants the money to go.
    store.fund_accounts["fa_usual"] = W.FundAccount(
        "fa_usual", v.known_account_number, v.known_ifsc, "cont_1", v.vendor_id)
    store.fund_accounts["fa_new"] = W.FundAccount(
        "fa_new", case["proposed_account_number"], case["proposed_ifsc"],
        "cont_1", v.vendor_id)
    return store


def fire(store, fund_account_id, payout_id, amount, doc_id=None, fav=None):
    """A genuinely signed payout.pending event through the real handler."""
    notes = {"payeeproof_document_id": doc_id} if doc_id else {}
    body = {
        "id": f"evt_{payout_id}", "entity": "event", "event": "payout.pending",
        "contains": ["payout"], "created_at": int(time.time()),
        "payload": {"payout": {"entity": {
            "id": payout_id, "entity": "payout",
            "fund_account_id": fund_account_id,
            "amount": int(round(amount * 100)), "currency": "INR",
            "status": "pending", "notes": notes}}},
    }
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return W.handle_payout_pending(raw, sig, store, secret=SECRET,
                                   fav_lookup=(lambda fa: fav) if fav else None,
                                   event_id_header=f"evt_{payout_id}")


def _replay_queue(store, rows):
    """
    Give every routed message the payout decision it is waiting for.

    WHY THIS EXISTS
    ===============
    The dashboard used to show 221 messages, 70 of them routed for review, and
    exactly THREE payout decisions — all against one demo vendor, only one of
    which corresponded to a message anyone could open. Two worlds that barely
    touched: a real mailbox from the corpus, and three hand-built payouts.

    The model was right and the demo was wrong. PayeeProof decides when money
    moves, not when mail arrives, so a change request that no payout has been
    attempted against genuinely has nothing to decide. But a queue where 69 of
    70 rows say "awaiting payment" teaches the viewer nothing, and the first
    question anyone asks is why they cannot open the others.

    So the payouts an accounts-payable team would actually have raised are
    raised here: for a change request, to the account the request asked for —
    which is exactly what a clerk acting on that email creates, and exactly the
    thing this system exists to judge. For routine mail, to the account the
    vendor already settles to.

    WHAT IS SIMULATED, STATED PLAINLY
    =================================
    The DESTINATION comes from what the message proposed. That is a fact about
    the message, not a label about the case — nothing here reads `label`, and
    the engine is never told which cases are fraudulent.

    FAV is replayed from the case row, schema-faithfully, exactly as
    eval/rules_eval.py does. The bank's answer is part of the world, not part
    of the policy.

    EXTRACTION is the perfect reading the rules eval uses, not a live model
    call. Seventy payouts would otherwise be seventy API calls every time the
    demo starts. The README reports what the real extractor costs against this
    same bound — nothing, on both splits — so the decisions shown here are the
    ones the live extractor produces, minus the wait.
    """
    import rules_eval as RE

    cases = {}
    with open(os.path.join(DATA, "cases_dev.csv"), newline="",
              encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cases[r["case_id"]] = r

    by_message = {r["message_id"]: r for r in rows}

    # The reading a flawless extractor would produce, keyed by the text the
    # handler will hand us. Anything not in here is routine mail: it names an
    # account without asking for anything to change, which is precisely what a
    # PAYMENT_FOLLOWUP is.
    readings = {}

    def extract_fn(text):
        return readings.get(text) or E.ExtractionResult(
            ok=True, intent=E.INTENT_FOLLOWUP, action=E.ACTION_NONE,
            scope=E.SCOPE_NONE, evidence_source="replayed_perfect_extraction")

    fired = 0
    for entry in list(store.triage_log):
        if entry.get("verdict") != "ROUTE" or not entry.get("document_id"):
            continue
        vendor = store.vendors.get(entry.get("vendor_id"))
        if vendor is None:
            continue

        row = by_message.get(entry["message_id"], {})
        case = cases.get(row.get("case_id") or "")

        if case:
            dest, ifsc = case["proposed_account_number"], case["proposed_ifsc"]
            amount = float(case["amount"])
            available = case["fav_name_available"] == "True"
            fav = W.FAVResult(
                case["fav_account_status"],
                case["registered_name_returned"] if available else None,
                int(case["name_match_score"]) if available else None)
            readings[entry.get("body") or ""] = RE.features_to_extraction(
                case, vendor)
        else:
            # Routine mail. The payout goes where this vendor is always paid,
            # and the bank confirms what the master already says.
            dest, ifsc = vendor.known_account_number, vendor.known_ifsc
            amount = vendor.avg_payout_amount or 25000.0
            fav = W.FAVResult("active", vendor.legal_name, 100)

        fa_id = f"fa_{fired:04d}"
        store.fund_accounts[fa_id] = W.FundAccount(
            fa_id, dest, ifsc, f"cont_{vendor.vendor_id}", vendor.vendor_id)
        # Always name the document, including for routine mail. The reading
        # returned for it is PAYMENT_FOLLOWUP / NONE — "this message asks for
        # nothing to change" — which is what sends it down R2 to be judged on
        # the real destination. Withholding the id left 53 of 69 messages
        # unable to reach their own decision, which is the confusion this
        # whole function exists to remove.
        _fire_replay(store, fa_id, f"pout_{fired:04d}", amount,
                     doc_id=entry["document_id"], fav=fav,
                     extract_fn=extract_fn)
        fired += 1
    return fired


def _fire_replay(store, fund_account_id, payout_id, amount, doc_id, fav,
                 extract_fn):
    """fire(), with the extractor supplied instead of bought."""
    notes = {"payeeproof_document_id": doc_id} if doc_id else {}
    body = {
        "id": f"evt_{payout_id}", "entity": "event", "event": "payout.pending",
        "contains": ["payout"], "created_at": int(time.time()),
        "payload": {"payout": {"entity": {
            "id": payout_id, "entity": "payout",
            "fund_account_id": fund_account_id,
            "amount": int(round(amount * 100)), "currency": "INR",
            "status": "pending", "notes": notes}}},
    }
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return W.handle_payout_pending(raw, sig, store, secret=SECRET,
                                   fav_lookup=lambda fa: fav,
                                   event_id_header=f"evt_{payout_id}",
                                   extract_fn=extract_fn)


def serve(store):
    """
    Hand THIS store to the dashboard.

    The alternative was telling the viewer to start uvicorn separately, which
    boots webhook_demo's fixtures — a different vendor, different payouts, and
    a queue that has nothing to do with the story just told. One command, one
    set of decisions.
    """
    import uvicorn
    import webhook

    os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", SECRET)

    # Feed the inbox view a real morning's mail. The triage log then shows what
    # was routed AND what was dropped, which is the part worth auditing — a
    # funnel that only displays its successes is not showing its work.
    import csv as _csv
    import triage as _T
    with open(os.path.join(DATA, "inbox_dev.csv"), newline="",
              encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    rows.sort(key=lambda r: float(r["received_at"]), reverse=True)
    for r in rows[:INBOX_MESSAGES]:
        store.ingest_message(_T.Message(
            message_id=r["message_id"], from_addr=r["from_addr"],
            subject=r["subject"], body=r["body"], thread_id=r["thread_id"],
            received_at=float(r["received_at"]),
            headers=_json.loads(r["headers"] or "{}"),
            in_reply_to=r["in_reply_to"]))

    fired = _replay_queue(store, rows[:220])

    print()
    rule("─")
    print(f"  Inbox loaded: {len(store.triage_log)} messages triaged.")
    print(f"  {fired} payouts raised against them, and decided.")
    print("  The operator dashboard:")
    print("    http://localhost:8000/inbox       the mailbox, routed and dropped")
    print("    http://localhost:8000/            the decision queue")
    print("    http://localhost:8000/case/pout_bec    the evidence table")
    print()
    print("  Every routed message now opens onto its own decision. The queue is")
    print("  the payouts an AP team would have raised from that mail: a change")
    print("  request pays where it asked to be paid, routine mail pays where it")
    print("  always has.")
    print()
    print("  Ctrl-C to stop.")
    rule("─")
    print()
    uvicorn.run(webhook.create_app(store), host="127.0.0.1", port=8000,
                log_level="warning")


def main():
    case, msg = load_case()
    cached_note = use_cached_extraction_if_no_key(CASE_ID)
    vendors = pipeline.load_vendors()
    v = vendors[case["vendor_id"]]
    store = build_store(vendors, case)

    print()
    rule("═")
    print("  PayeeProof — one payout, end to end")
    rule("═")
    beat(f"vendor        {v.legal_name}  ({v.vendor_id})")
    beat(f"domain        {v.known_domain}")
    beat(f"paid into     {v.known_account_number}  since {v.accounts[0].added_on}")
    beat(f"              {v.accounts[0].settled_payout_count} settled payouts")
    beat(f"mailbox       {len(store.inbox._messages):,} messages on file")
    beat(f"extraction    {cached_note or 'live — reading the message now'}")

    # ── 1 ────────────────────────────────────────────────────────────
    scene(1, "A routine payout. Nobody is involved.")
    r = fire(store, "fa_usual", "pout_routine", 14670.93)
    beat(f"payout to {v.known_account_number} — the account on file")
    beat()
    beat(f"  rule fired      {r.audit['decision']['rule_fired']}")
    beat(f"  outcome         {r.outcome}")
    beat(f"  released        {r.audit['payout_allowed']}")
    beat()
    beat("This is the common case. No email, no phone call, no person — the")
    beat("destination matches the vendor master and the money goes.")

    # ── 2 ────────────────────────────────────────────────────────────
    scene(2, "An email arrives asking to change the bank details.")
    beat(f"From:    {msg['from_addr']}")
    beat(f"Subject: {msg['subject']}")
    beat()
    quote("\n".join(msg["body"].split("\n")[:4]))
    beat()
    beat(f"The real domain is {v.known_domain}.")
    beat(f"This came from {msg['from_addr'].split('@')[1]}.")

    # ── 3 ────────────────────────────────────────────────────────────
    scene(3, "Triage. 7,176 messages a day; this one has to survive.")
    out = store.ingest_message(T.Message(
        message_id=msg["message_id"], from_addr=msg["from_addr"],
        subject=msg["subject"], body=msg["body"], thread_id=msg["thread_id"],
        received_at=float(msg["received_at"])))
    beat(f"  verdict         {out['verdict']}")
    beat(f"  resolved to     {out['vendor_id']}  by {out['match'].upper()} match")
    beat()
    beat("A sender allowlist would have DROPPED this — the forged domain is not")
    beat("in the vendor master, which is the point of forging it. Measured on the")
    beat("full inbox, an allowlist discards 64.6% of all the fraud.")
    beat()
    beat("It resolved because the body quotes this vendor's real GST number.")
    beat()
    beat("  What the MCP inbox tools returned:")
    for sig in out["inbox_signals"]:
        beat(f"    signal          {sig}")
    hist = store.inbox.search_history(msg["from_addr"].split("@")[1],
                                      before=float(msg["received_at"]))
    known = store.inbox.search_history(v.known_domain)
    beat(f"    prior mail from this sender      {len(hist)}")
    beat(f"    prior mail from {v.known_domain:<16} {len(known)}")

    # ── 4 ────────────────────────────────────────────────────────────
    scene(4, "The payout goes pending. The money is frozen right now.")
    fav = W.FAVResult(account_status=case["fav_account_status"],
                      registered_name=case["registered_name_returned"],
                      name_match_score=int(case["name_match_score"]))
    r = fire(store, "fa_new", "pout_bec", float(case["amount"]),
             doc_id=out["document_id"], fav=fav)
    d = r.audit["decision"]
    beat(f"destination     {case['proposed_account_number']}  "
         f"(from the payout, not the email)")
    beat(f"amount          Rs {float(case['amount']):,.2f}")
    beat(f"evidence        {r.audit['extraction'].get('evidence_source')}")
    beat()
    for tier, label in ((d["tier1"], "identity"), (d["tier2"], "context")):
        for s in tier:
            flag = "  <- DECEPTION" if s.get("deception") else ""
            beat(f"  {s['result']:13s} {s['name']:32s} {label}{flag}")
    beat()
    beat(f"  rule fired      {d['rule_fired']}")
    beat(f"  outcome         {r.outcome}")
    beat(f"  released        {r.audit['payout_allowed']}")
    beat()
    beat("Note which rule caught it. Not the impersonation rule — this domain is")
    beat("too far from the real one to register as a typosquat, so there is no")
    beat("deception signal. It was held on accumulated doubt: an unfamiliar")
    beat("sender, a new destination, urgency, and a first contact.")
    beat()
    beat("That is the honest shape of most BEC. The textbook case is easy; this")
    beat("one is held because nothing could be confirmed, not because something")
    beat("was proven.")

    # ── 5 ────────────────────────────────────────────────────────────
    scene(5, "What it would take to release it.")
    controls = [a for a in (case["requester_controls_accounts"] or "").split(";") if a]
    named, basis = verifier.select_verification_account(v, case["request_date"])
    beat("Two channels, and they are not equal.")
    beat()
    beat(f"  1  call {v.known_phone} — the number in the vendor master,")
    beat("     never a number from the email.")
    beat(f"     answered: {case['callback_reaches_known_contact']}")
    beat()
    beat(f"  2  Rs 1 from {named.account_number if named else 'NOTHING QUALIFIES'}")
    beat(f"     the system NAMES the account. The requester never chooses.")
    beat(f"     {basis}")
    beat(f"     requester can send from: {controls or 'nothing'}")
    beat()
    beat("Channel 2 is authoritative. Taking a mailbox is easy and taking a phone")
    beat("number is possible; sending money OUT of the victim's own account is")
    beat("not, because moving money away from it is the whole point of the fraud.")

    # ── 6 ────────────────────────────────────────────────────────────
    scene(6, "What the operator sees.")
    for a in r.audit["razorpay_actions"]:
        if a["method"]:
            flag = ("   [WAITS FOR A HUMAN]"
                    if a.get("requires_human_confirmation") else "")
            beat(f"  {a['method']:6s} {a['endpoint']}{flag}")
        else:
            beat(f"  {a['effect']}")
    beat()
    beat("Nothing here calls Razorpay. These are the calls a human confirms.")
    beat()
    beat("The queue an operator actually works, rendered from this decision:")
    beat()
    block(dashboard_preview(store), indent=8)

    # ── 7 ────────────────────────────────────────────────────────────
    scene(7, "The other outcome: when there IS hard evidence.")
    other = next(x for x in vendors.values() if x.vendor_id != v.vendor_id)
    store.fund_accounts["fa_mule"] = W.FundAccount(
        "fa_mule", other.known_account_number, other.known_ifsc,
        "cont_1", v.vendor_id)
    r2 = fire(store, "fa_mule", "pout_mule", 14670.93)
    d2 = r2.audit["decision"]
    beat(f"A payout for {v.vendor_id} pointed at an account already on file")
    beat(f"for {other.vendor_id}. One account collecting from several suppliers")
    beat("is how one attacker harvests many victims.")
    beat()
    beat(f"  rule fired      {d2['rule_fired']}")
    beat(f"  outcome         {r2.outcome}")
    beat(f"  recommends      {str(d2.get('recommended_action')).upper()}")
    beat()
    for a in r2.audit["razorpay_actions"]:
        if a["method"]:
            flag = ("   [WAITS FOR A HUMAN]"
                    if a.get("requires_human_confirmation") else "")
            beat(f"  {a['method']:6s} {a['endpoint']}{flag}")
    beat()
    beat("Even here the engine only RECOMMENDS. v1 would have cancelled this")
    beat("payout by itself, and was wrong about 2.2% of the ones it cancelled.")
    beat("There is no rejection outcome left in the engine; a test asserts it.")

    if "--serve" not in sys.argv:
        print()
        rule("─")
        print("  To open the dashboard on these same decisions:")
        print("    python src/demo.py --serve")

    print()
    rule("═")
    print("  Held, not rejected. The payout sits pending until a person acts.")
    print("  On 278 held-out cases: 100% of fraud held, 0% of legitimate")
    print("  payments cancelled. v1 cancelled 2.2% of them outright.")
    rule("═")

    if "--serve" in sys.argv:
        serve(store)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
