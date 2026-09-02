"""
PayeeProof — operator dashboard.

A live view of decisions as payout.pending events arrive, mounted on the same
FastAPI app as the webhook so a demo shows the decision the moment it is made.

WHY IT SHOWS MORE THAN THE WEBHOOK RESPONSE DOES
================================================
The webhook reply deliberately carries no audit record and no reason string,
because reason strings embed the destination account number and the vendor's
known accounts, and that reply goes back over the public internet.

The dashboard has a different audience: an operator inside the merchant, behind
whatever authentication the deployment puts in front of it. So it may show the
full signal table — that is the point of it. The difference is deliberate, and
worth keeping deliberate: if this is ever exposed publicly it needs auth in
front, exactly like /documents does.

WHAT IT IS FOR
The interesting thing about this system is not that it returns a verdict, it is
that every verdict is attributable. The case view shows each signal, what it
found, and where the evidence came from — so a held payout can be explained to
the vendor whose money it is.

Server-rendered HTML, no build step, no new dependencies.
"""

import html
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import casefile
import vocabulary as V

OUTCOME_CLASS = {
    "ALLOW": "allow",
    "STEP_UP_VERIFY": "hold",
    "BLOCK": "block",
    "HELD": "hold",
    "DUPLICATE": "muted",
}

RESULT_CLASS = {
    "PASS": "ok",
    "WARN": "warn",
    "INCONCLUSIVE": "unknown",
    "FAIL": "fail",
}

CSS = """
:root{
  --bg:#0f1319; --panel:#161c24; --panel-2:#1c242e; --line:#28323e;
  --ink:#e7edf4; --dim:#93a1b1; --faint:#667382;
  --ok:#5fb98a; --warn:#d9a048; --unknown:#7c8ea3; --fail:#e4776f;
  --accent:#4fbdb4;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1060px;margin:0 auto;padding:28px 22px 80px}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}
h1 span{color:var(--accent)}
.sub{color:var(--dim);font-size:13px}
.spacer{flex:1}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--dim);margin:26px 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--faint);font-weight:600;padding:0 12px 8px 0;white-space:nowrap}
td{border-top:1px solid var(--line);padding:10px 12px 10px 0;vertical-align:top}
tr:hover td{background:var(--panel)}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px;
  font-variant-numeric:tabular-nums}
.pill{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11px;
  font-weight:600;padding:2px 8px;border-radius:3px;letter-spacing:.04em;white-space:nowrap}
.pill.allow{background:#14261c;color:var(--ok);border:1px solid var(--ok)}
.pill.hold{background:#2a2013;color:var(--warn);border:1px solid var(--warn)}
.pill.block{background:#2c1817;color:var(--fail);border:1px solid var(--fail)}
.pill.muted{background:var(--panel-2);color:var(--dim);border:1px solid var(--line)}
.tag{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:10.5px;
  padding:1px 6px;border-radius:2px;border:1px solid var(--line);color:var(--dim)}
.sig{display:grid;grid-template-columns:80px 170px 1fr;gap:0;
  border:1px solid var(--line);border-radius:4px;overflow:hidden;margin-bottom:18px}
.sig>div{padding:9px 12px;border-top:1px solid var(--line);background:var(--panel)}
.sig>div:nth-child(-n+3){border-top:none;background:var(--panel-2);
  font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint)}
.res{font-family:ui-monospace,Menlo,monospace;font-size:11px;font-weight:600}
.res.ok{color:var(--ok)} .res.warn{color:var(--warn)}
.res.unknown{color:var(--unknown)} .res.fail{color:var(--fail)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  padding:16px 18px;margin-bottom:18px}
.kv{display:grid;grid-template-columns:180px 1fr;gap:7px 16px;font-size:13px}
.kv dt{color:var(--dim)}
.kv dd{margin:0;font-family:ui-monospace,Menlo,monospace;font-size:12px}
.empty{color:var(--dim);padding:40px 0;text-align:center;border:1px dashed var(--line);
  border-radius:4px}
.flag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:10px;
  margin:2px 4px 2px 0;border:1px solid transparent;white-space:nowrap}
.flag.warn{background:rgba(217,160,72,.14);color:var(--warn);
  border-color:rgba(217,160,72,.3)}
.flag.bad{background:rgba(228,119,111,.14);color:var(--fail);
  border-color:rgba(228,119,111,.35)}
.flag{background:var(--panel-2);color:var(--dim);border-color:var(--line)}
.mail{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;
  padding:16px 18px;white-space:pre-wrap;line-height:1.6;
  font-size:13.5px;color:var(--ink);overflow-x:auto}
.mailhead{display:grid;grid-template-columns:88px 1fr;gap:4px 12px;
  font-size:13px;margin-bottom:12px}
.mailhead dt{color:var(--faint)}
.mailhead dd{margin:0;color:var(--ink);font-family:ui-monospace,Menlo,monospace}
.step{border-left:3px solid var(--accent);padding:10px 14px;margin:12px 0;
  background:var(--panel-2);border-radius:0 6px 6px 0}
.demand{border:1px solid var(--accent);border-radius:8px;padding:14px 16px;
  margin-bottom:14px;background:rgba(79,189,180,.07)}
.demand.unavailable{border-color:var(--warn);background:rgba(217,160,72,.07)}
.demand .acct{font-family:ui-monospace,Menlo,monospace;font-size:1.45rem;
  letter-spacing:.04em;color:var(--ink);margin:6px 0}
.duties{margin-top:14px;padding:11px 14px;border-left:3px solid var(--warn);
  background:var(--panel-2);border-radius:0 6px 6px 0;font-size:.9rem;
  color:var(--dim)}
.duties strong{color:var(--ink)}
.statebar{border-left:3px solid var(--accent)}
.refused{border:1px solid var(--fail);background:rgba(228,119,111,.1);
  border-radius:6px;padding:12px 16px;margin-bottom:18px;color:var(--ink)}
.actorpick{display:inline-block;font-size:13px;color:var(--dim);margin-bottom:6px}
.actorpick select{margin-left:8px;background:var(--panel-2);color:var(--ink);
  border:1px solid var(--line);border-radius:5px;padding:6px 9px;font:inherit;
  font-size:13px}
.bgroup{border-top:1px solid var(--line);padding-top:14px;margin-top:14px}
.blabel{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--faint);font-weight:600;margin-bottom:6px}
.btn{font:inherit;font-size:13px;padding:8px 14px;margin:6px 8px 0 0;
  border-radius:6px;cursor:pointer;background:var(--panel-2);color:var(--ink);
  border:1px solid var(--line)}
.btn:hover{border-color:var(--accent)}
.btn.good{border-color:rgba(95,185,138,.5);color:var(--ok)}
.btn.good:hover{background:rgba(95,185,138,.12)}
.btn.danger{border-color:rgba(228,119,111,.5);color:var(--fail)}
.btn.danger:hover{background:rgba(228,119,111,.12)}
textarea{width:100%;background:var(--panel-2);color:var(--ink);font:inherit;
  font-size:13px;border:1px solid var(--line);border-radius:6px;padding:9px 11px;
  resize:vertical}
.hist{display:grid;grid-template-columns:150px 1fr;gap:12px 16px;font-size:13px}
.hwhen{color:var(--faint);font-family:ui-monospace,Menlo,monospace;font-size:12px}
.mrow{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:baseline}
.msub{color:var(--faint);font-size:.82rem}
.drop{opacity:.62}
.note{color:var(--faint);font-size:12px;margin-top:6px}
.reason{color:var(--dim);font-size:12.5px;margin-top:4px;max-width:80ch}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim)}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _link(message_id: Any) -> str:
    """
    A message id is safe to display and not safe to paste into a URL.

    Real ids are angle-bracketed — <CAF...@mail.example.com> — and < > are not
    valid in a path. HTML-escaping alone renders them correctly and produces a
    link that does not resolve, so the id is percent-encoded first and escaped
    second. Starlette decodes it back before the lookup.
    """
    return _e("/message/" + quote(str(message_id or ""), safe=""))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body>"
        f"<div class=\"wrap\">{body}</div></body></html>"
    )


def _header(sub: str, extra: str = "") -> str:
    return (
        "<header><h1>Payee<span>Proof</span></h1>"
        f"<div class=\"sub\">{_e(sub)}</div><div class=\"spacer\"></div>{extra}</header>"
    )


def _outcome_pill(outcome: str) -> str:
    """
    The word an operator reads, with the internal code kept as the tooltip.

    Auditors need the exact outcome the engine emitted; operators need to know
    whether the money moved. Showing only one of those trades a readable screen
    for an unauditable one, or the reverse.
    """
    return (f"<span class=\"pill {OUTCOME_CLASS.get(outcome, 'muted')}\" "
            f"title=\"{_e(outcome)}\">{_e(V.outcome(outcome))}</span>")


# Rarest and most decisive first. A chip that appears on nine rows in ten is
# wallpaper — the operator stops seeing chips at all — so the ones that separate
# a few messages from the rest have to lead.
FLAG_ORDER = [
    "inbox_first_contact",
    "inbox_thread_shallow",
]

# When two chips would say the same thing, keep the one that says WHY. Triage's
# "content" match and the inbox_sender_unrecognised signal are the same fact
# arrived at twice, and showing both taught the reader that chips are padding.
SUPPRESSED_BY_SIGNAL = {"inbox_sender_unrecognised"}


def _flag_list(row: Dict[str, Any]):
    """
    Why this message is worth a second look, as (severity, label) pairs.

    Every routed message carries something — otherwise triage would not have
    routed it — so the queue should say what, rather than leaving seventy rows
    looking identical until a payout happens to arrive.
    """
    findings = {f.get("name"): f for f in (row.get("inbox_findings") or [])}
    names = list(findings) or list(row.get("inbox_signals") or [])
    flags = []

    if row.get("recommended_action") == "reject":
        flags.append(("bad", "Recommend reject"))

    match = row.get("match")
    if match == "lookalike":
        flags.append(("bad", "Lookalike domain"))
    elif match == "content" and not (set(names) & SUPPRESSED_BY_SIGNAL):
        flags.append(("warn", "Unrecognised sender"))

    def rank(n):
        return FLAG_ORDER.index(n) if n in FLAG_ORDER else -1

    for name in sorted(names, key=rank):
        flags.append(("warn", V.signal(name)))
    return flags


def _risk_flags(row: Dict[str, Any]) -> str:
    return "".join(f'<span class="flag {c}">{_e(t)}</span>'
                   for c, t in _flag_list(row))


def _humanise(text: Any) -> str:
    """
    The engine's own reason sentence, with its signal names spelled out.

    The reason string is built for the audit record and names signals the way
    the rule table does — `account_continuity`, `gstin`. That is right for the
    record and wrong for the one sentence an operator reads first: it looks
    like a stack trace and reads as though the screen is talking to a
    programmer. Only known names are substituted, so an unrecognised one comes
    through as itself rather than being mangled into something plausible.
    """
    out = str(text or "")
    for name in sorted(V.SIGNAL, key=len, reverse=True):
        out = out.replace(name, V.SIGNAL[name])
    return out


def _findings_panel(row: Dict[str, Any]) -> str:
    """The mailbox history behind the chips, in full, with its provenance."""
    findings = row.get("inbox_findings") or []
    if not findings:
        return ""
    body = '<h2>What the mailbox history shows</h2><div class="sig">'
    body += "<div>result</div><div>check</div><div>finding</div>"
    body += _signal_rows_v2(findings)
    body += "</div>"
    body += ('<p class="note">Everything here is Tier 2. A mailbox owner can '
             'send themselves messages and build a thread to any depth, so this '
             'evidence may hold a payment and can never release one.</p>')
    return body


def _recommendation(decision: Dict[str, Any]) -> str:
    """
    The engine no longer rejects anything by itself, so the recommendation is
    the only place a reviewer sees that it wanted to. Showing the hold without
    it would make a BEC case and a routine unfamiliar-account hold look
    identical in the queue.
    """
    if decision.get("recommended_action") != "reject":
        return ""
    return "<span class=\"pill block\" title=\"awaiting human confirmation\">RECOMMEND REJECT</span>"


def render_index(audits: List[Dict[str, Any]]) -> str:
    if not audits:
        body = _header("no decisions yet")
        body += ("<div class=\"empty\">No payout.pending events received.<br>"
                 "<code>python src/webhook_demo.py</code> drives five signed "
                 "scenarios, or POST a signed event to "
                 "<code>/webhooks/razorpay</code>.</div>")
        return _page("PayeeProof — decisions", body)

    held = sum(1 for a in audits if not a.get("payout_allowed"))
    rec = sum(1 for a in audits
              if a.get("decision", {}).get("recommended_action") == "reject")
    body = _header(f"{len(audits)} decisions · {held} not released"
                   + (f" · {rec} recommended for rejection" if rec else ""),
                   "<a href=\"/inbox\">← inbox</a>")
    body += "<h2>Decisions, newest first</h2><table><thead><tr>"
    for h in ("payout", "vendor", "destination", "document", "rule", "outcome"):
        body += f"<th>{h}</th>"
    body += "</tr></thead><tbody>"

    for a in audits:
        d = a.get("decision", {})
        dest = a.get("destination", {})
        doc = a.get("document", {})
        verified = dest.get("source") == "razorpay_fund_account"
        body += (
            "<tr>"
            f"<td class=\"mono\"><a href=\"/case/{_e(a.get('payout_id'))}\">"
            f"{_e(a.get('payout_id'))}</a></td>"
            f"<td class=\"mono\">{_e(a.get('vendor_id'))}</td>"
            f"<td class=\"mono\">{_e(dest.get('account_number'))}"
            f"<div class=\"note\">{'from the payout' if verified else 'UNVERIFIED'}</div></td>"
            f"<td class=\"mono\">{_e(doc.get('correlation'))}</td>"
            f"<td class=\"mono\">{_e(d.get('rule_fired'))}</td>"
            f"<td>{_outcome_pill(a.get('final_outcome', '?'))}"
            f"{_recommendation(d)}</td>"
            "</tr>"
        )
    body += "</tbody></table>"
    body += ("<p class=\"note\">Every row is a payout that was frozen when the "
             "decision was made. Open one to see the evidence behind it.</p>")
    return _page("PayeeProof — decisions", body)


def _signal_rows_v2(sigs: List[Dict[str, Any]]) -> str:
    out = ""
    for sg in sigs:
        cls = RESULT_CLASS.get(sg.get("result"), "unknown")
        flag = ("<span class=\"flag bad\">deliberate impersonation</span>"
                if sg.get("deception") else "")
        out += (f"<div class=\"{cls}\" title=\"{_e(sg.get('result'))}\">"
                f"{_e(V.result(sg.get('result')))}</div>"
                f"<div><strong>{_e(V.signal(sg.get('name')))}</strong> {flag}"
                f"<div class=\"note mono\">{_e(sg.get('name'))}</div></div>"
                f"<div>{_e(sg.get('detail'))}"
                f"<div class=\"note\">from {_e(V.source(sg.get('source')))}</div>"
                f"</div>")
    return out


def _signal_rows(signals: List[Dict[str, Any]]) -> str:
    out = ""
    for s in signals or []:
        cls = RESULT_CLASS.get(s.get("result"), "unknown")
        flag = " <span class=\"tag\">deception</span>" if s.get("deception") else ""
        out += (
            f"<div class=\"res {cls}\">{_e(s.get('result'))}</div>"
            f"<div class=\"mono\">{_e(s.get('name'))}{flag}</div>"
            f"<div>{_e(s.get('detail'))}"
            f"<div class=\"note\">source: {_e(s.get('source'))}</div></div>"
        )
    return out


MATCH_NOTE = {
    "exact":     "sender domain is in the vendor master",
    "lookalike": "sender domain is built to be mistaken for a known one",
    "content":   "sender is unknown — matched only on an identifier in the body",
}


def _when(ts: Any) -> str:
    """A timestamp an operator can read, not an epoch float."""
    try:
        return time.strftime("%d %b %Y, %H:%M", time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return "—"


def _status_cell(r: Dict[str, Any]) -> str:
    """
    Where this message stands, in the operator's terms.

    A routed message with no payout is not "nothing happened" — it is a change
    request that has been read and is waiting for money to be attempted against
    it. Showing an empty cell for that state was why the queue looked as though
    only one message had ever been flagged.
    """
    if r.get("final_outcome"):
        rec = ('<span class="pill block">Recommend reject</span>'
               if r.get("recommended_action") == "reject" else "")
        link = ('<div class="note"><a href="/case/%s">Open the decision &rarr;</a></div>'
                % _e(r.get("payout_id")))
        # Where the human work stands, when there is any. A queue that shows
        # only the engine's verdict cannot distinguish "nobody has touched
        # this" from "called, verified, waiting on a second person" — which
        # are different jobs for different people.
        case = r.get("case") or {}
        worked = ""
        if case.get("state") not in (None, "open", "no_action"):
            worked = (f'<div class="note">{_e(case.get("label"))}</div>')
        return _outcome_pill(r["final_outcome"]) + rec + worked + link
    if r.get("verdict") == "ROUTE":
        return ('<span class="pill muted" title="no payout.pending event yet">'
                'Awaiting payment</span>'
                '<div class="note">Read and on file. PayeeProof decides when a '
                'payment is attempted against it.</div>')
    return '<span class="pill muted">Filtered out</span>'


def render_inbox(rows: List[Dict[str, Any]]) -> str:
    """
    The mailbox, as triage saw it.

    Every message is openable, including the ones triage dropped. That is not a
    convenience: the failure this layer is most exposed to is silently binning a
    real change request, and a reviewer can only check that by reading the ones
    it binned. A queue that only opens its own hits cannot be audited by anyone
    who does not already trust it.
    """
    if not rows:
        body = _header("inbox empty")
        body += ('<div class="empty">No messages triaged yet.<br>'
                 'POST one to <code>/messages</code>, or run '
                 '<code>python src/demo.py --serve</code>.</div>')
        return _page("PayeeProof — inbox", body)

    routed = [r for r in rows if r.get("verdict") == "ROUTE"]
    dropped = [r for r in rows if r.get("verdict") != "ROUTE"]
    open_now = [r for r in routed if r.get("final_outcome") in
                ("STEP_UP_VERIFY", "HELD")]

    body = _header(f"{len(rows)} messages · {len(routed)} need review · "
                   f"{len(open_now)} waiting on you",
                   '<a href="/">Decisions &rarr;</a>')

    body += ('<p class="note">Everything the mailbox delivered. Triage matches '
             'the sender against your supplier records with no model call; only '
             'what survives is read in full. Open any message — including the '
             'ones that were filtered out.</p>')

    def row_html(r, drop=False):
        cls = ' class="drop"' if drop else ""
        mid = r.get("message_id") or ""
        subject = r.get("subject") or "(no subject)"
        flags = _risk_flags(r)
        supplier = r.get("vendor_id") or "not matched"
        note = MATCH_NOTE.get(r.get("match") or "", "")
        # A routed message with no adverse flag is not a mystery: it is here
        # because it named a payment destination. Falling back to WHO the
        # sender is answered a different question than the column asks.
        why = flags or _e(_humanise(r.get("reason"))) or _e(note) or "—"
        third = (f'<td>{why}</td>' if not drop else
                 f'<td>{_e(V.verdict(r.get("verdict")))}'
                 f'<div class="note">{_e((r.get("reason") or "")[:90])}</div></td>')
        return (
            f"<tr{cls}>"
            f'<td><a href="{_link(mid)}">{_e(subject)}</a>'
            f'<div class="msub mono">{_e(r.get("from"))}</div>'
            f'<div class="msub">{_when(r.get("received_at"))}</div></td>'
            f'<td>{_e(supplier)}'
            f'<div class="note">{_e(note)}</div></td>'
            f"{third}"
            f"<td>{_status_cell(r)}</td>"
            "</tr>")

    body += f"<h2>Needs review — {len(routed)}</h2>"
    body += "<table><thead><tr>"
    for h in ("Message", "Supplier", "Why it is here", "Status"):
        body += f"<th>{h}</th>"
    body += "</tr></thead><tbody>"
    body += "".join(row_html(r) for r in routed) or \
        '<tr><td colspan="4" class="note">none</td></tr>'
    body += "</tbody></table>"

    by_verdict: Dict[str, int] = {}
    for r in dropped:
        by_verdict[r.get("verdict")] = by_verdict.get(r.get("verdict"), 0) + 1
    summary = " · ".join(f"{v} {V.verdict(k).lower()}"
                         for k, v in sorted(by_verdict.items()))

    body += f"<h2>Filtered out — {_e(summary or 'none')}</h2>"
    body += ('<p class="note">A filtered message is not a released payment. The '
             'payout event still arrives; with no change request on file the '
             'engine judges the real destination, so an account you have paid '
             "before passes, an unseen one is held, and another supplier's "
             'account is still caught.</p>')
    body += "<table><thead><tr>"
    for h in ("Message", "Supplier", "Filtered because", "Status"):
        body += f"<th>{h}</th>"
    body += "</tr></thead><tbody>"
    body += "".join(row_html(r, drop=True) for r in dropped[:60]) or \
        '<tr><td colspan="4" class="note">none</td></tr>'
    body += "</tbody></table>"
    if len(dropped) > 60:
        body += f'<p class="note">{len(dropped) - 60} more not shown.</p>'

    return _page("PayeeProof — inbox", body)


def render_message(r: Optional[Dict[str, Any]]) -> str:
    """
    One message, as the operator receives it — the mail first, the machine's
    reading second.

    The order is the point. An operator who reads a verdict before the email
    inherits the machine's conclusion; an operator who reads the email first can
    disagree with it. Since the whole system is built so a human makes the final
    call, the screen has to leave room for that human to actually form a view.
    """
    if r is None:
        body = _header("message not found")
        body += ('<div class="empty">No message with that id.<br>'
                 '<a href="/inbox">Back to the inbox</a></div>')
        return _page("PayeeProof — not found", body)

    subject = r.get("subject") or "(no subject)"
    body = _header(subject, '<a href="/inbox">&larr; Inbox</a>')

    body += '<div class="card"><dl class="mailhead">'
    for label, value in (
        ("From", r.get("from")),
        ("Received", _when(r.get("received_at"))),
        ("Subject", subject),
        ("Reply to", r.get("in_reply_to") or "not a reply"),
        ("Thread", r.get("thread_id") or "none"),
    ):
        body += f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>"
    body += "</dl>"
    flags = _risk_flags(r)
    if flags:
        body += f"<div>{flags}</div>"
    body += "</div>"

    body += "<h2>The message</h2>"
    body += f'<div class="mail">{_e(r.get("body") or "(empty)")}</div>'

    body += _findings_panel(r)

    # ── what triage did with it ──────────────────────────────────────
    match_label, match_why = V.match(r.get("match"))
    body += '<h2>What the sender resolved to</h2><div class="card"><dl class="kv">'
    for label, value in (
        ("Supplier", r.get("vendor_id") or "no supplier matched"),
        ("Matched on", match_label),
        ("Because", match_why or "—"),
        ("Domain matched", r.get("matched_domain") or "—"),
        ("Triage decision", V.verdict(r.get("verdict"))),
        ("Change request filed", r.get("document_id") or "none"),
    ):
        body += f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>"
    body += "</dl>"
    if r.get("reason"):
        body += f'<div class="reason">{_e(_humanise(r.get("reason")))}</div>'
    body += ('<div class="note">Sender matching decides whether a message is '
             'read in full. It never decides a payment on its own — a lookalike '
             'domain is a reason to look, not a reason to reject.</div>')
    body += "</div>"

    # ── what the engine decided, if a payment has been attempted ─────
    a = r.get("audit")
    if a:
        d = a.get("decision") or {}
        rule_fired = d.get("rule_fired")
        body += '<h2>What happens to the payment</h2><div class="card">'
        body += ('<div style="margin-bottom:10px">'
                 f'{_outcome_pill(a.get("final_outcome", "?"))} '
                 f'{_recommendation(d)}</div>')
        body += f'<div><strong>{_e(V.rule(rule_fired))}</strong>'
        body += f'<div class="note mono">{_e(rule_fired)}</div></div>'
        step = V.next_step(rule_fired)
        if step:
            body += f'<div class="step">{_e(step)}</div>'
        body += (f'<div class="note"><a href="/case/{_e(a.get("payout_id"))}">'
                 'Open the full decision, every check and what to ask for '
                 '&rarr;</a></div>')
        body += "</div>"
    elif r.get("verdict") == "ROUTE":
        body += '<h2>What happens to the payment</h2><div class="card">'
        body += ('<div><span class="pill muted">Awaiting payment</span></div>'
                 '<div class="reason">This request has been read and filed. '
                 'PayeeProof decides when a payment is actually attempted '
                 'against this supplier, because the destination it judges is '
                 'the one on the payout — never the one typed in the email.'
                 '</div>')
        body += "</div>"

    return _page(f"PayeeProof — {subject}", body)


STATE_CLASS = {
    "released": "allow", "rejected": "block", "verified": "allow",
    "contested": "block", "awaiting_proof": "hold", "contacting": "hold",
    "open": "hold", "no_action": "muted",
}


def _actor_select(actor: str) -> str:
    opts = ""
    for name, role in casefile.OPERATORS:
        sel = " selected" if name == actor else ""
        opts += f'<option value="{_e(name)}"{sel}>{_e(name)} — {_e(role)}</option>'
    return (f'<label class="actorpick">Acting as '
            f'<select name="actor">{opts}</select></label>')


def _buttons(codes, cls: str) -> str:
    out = ""
    for code in codes:
        label, meaning = casefile.ACTIONS[code]
        out += (f'<button class="btn {cls}" name="action" value="{_e(code)}" '
                f'title="{_e(meaning)}">{_e(label)}</button>')
    return out


def _case_actions_panel(payout_id: str, actions, actor: str,
                        demand_account, final_outcome) -> str:
    """
    The buttons, and what they are actually for.

    They do not move money and they are not "approve" and "decline". Each one
    records a fact a human established OUTSIDE this system — a call placed on a
    number the supplier did not choose, a rupee that arrived from an account we
    already trusted — because that is the only kind of evidence this problem can
    be settled with. The engine cannot make a phone call; what it can do is
    refuse to let the record of that call be written and acted on by the same
    person.

    Release is drawn enabled even when the current operator may not use it. The
    refusal is the demonstration: pressing it returns the reason from the server,
    which is where the rule lives. A control that is only a disabled button is
    not a control, and drawing it disabled would hide that the check is real.
    """
    state = casefile.state_of(actions, final_outcome)
    if state == "no_action":
        return ""

    resolved = state in ("released", "rejected")
    body = '<h2>Record what you did</h2><div class="card">'

    if resolved:
        body += ('<div class="note">This case is closed. The history below is '
                 'kept as the record of how it was closed.</div></div>')
        return body

    body += f'<form method="post" action="/case/{_e(payout_id)}/action">'
    body += _actor_select(actor)
    body += ('<div class="note">Every record below names the person who made '
             'it. That is what makes the two-person rule enforceable.</div>')

    body += '<div class="bgroup"><div class="blabel">The phone call</div>'
    body += ('<div class="note">Ring the number in our supplier records. Never '
             'a number written in the request — a request that can change the '
             'account can change the phone number under it.</div>')
    body += _buttons(["callback_requested"], "neutral")
    body += _buttons(["callback_confirmed"], "good")
    body += _buttons(["callback_unreachable", "callback_contested"], "neutral")
    body += _buttons(["callback_denied"], "danger")
    body += "</div>"

    body += '<div class="bgroup"><div class="blabel">The rupee</div>'
    if demand_account:
        body += ('<div class="note">Ask the supplier to send Rs 1 from '
                 f'<strong>{_e(demand_account)}</strong>, and no other account. '
                 'Which account is asked for is the entire control.</div>')
        body += (f'<input type="hidden" name="detail" '
                 f'value="{_e(demand_account)}">')
        body += _buttons(["proof_requested"], "neutral")
        body += _buttons(["proof_received"], "good")
        body += _buttons(["proof_not_received"], "danger")
    else:
        body += ('<div class="note">No account qualifies to be asked, so this '
                 'channel is unavailable on this case. It does not fall back to '
                 'the phone call — that is the point of tracking it separately.'
                 '</div>')
    body += "</div>"

    body += '<div class="bgroup"><div class="blabel">Note (optional)</div>'
    body += ('<textarea name="note" rows="2" placeholder="What was said, who '
             'answered, anything the next person needs."></textarea></div>')

    body += '<div class="bgroup resolve"><div class="blabel">Close the case</div>'
    ok_rel, why_rel = casefile.may_release(actions, actor, final_outcome)
    body += _buttons(["released"], "good")
    body += _buttons(["rejected"], "danger")
    if not ok_rel:
        body += f'<div class="note">Release as {_e(actor)}: {_e(why_rel)}</div>'
    body += ('<div class="note">Rejecting needs no second person. The two-person '
             'rule protects money leaving; refusing to pay releases nothing.'
             '</div>')
    body += "</div></form></div>"
    return body


def _accounts_panel(a: Dict[str, Any]) -> str:
    """
    The accounts on file, and how each one got there.

    Deliberately not a browsable vendor master. It shows the accounts THIS
    decision turned on, because the master is the thing being attacked and a
    screen that invites an operator to eyeball it and conclude the request
    looks fine is the exact reasoning a poisoned trust store defeats.

    What it does buy: "on file but never established" stops being a sentence
    the operator has to take on trust. An account added because somebody sent
    an email, never verified by anything outside email, and never used to pay
    anyone, looks different on the page from one that has settled thirty-nine
    payouts since onboarding — which is the whole of the engine's reasoning,
    made visible rather than asserted.
    """
    rows = a.get("accounts_on_file") or []
    if not rows:
        return ""

    body = "<h2>Accounts on file for this supplier</h2><div class=\"card\">"
    body += "<table><thead><tr>"
    for h in ("Account", "Added", "Verified", "Settled", ""):
        body += f"<th>{h}</th>"
    body += "</tr></thead><tbody>"

    for r in rows:
        marks = ""
        if r.get("is_destination"):
            marks += '<span class="flag warn">This payment</span>'
        if r.get("is_primary"):
            marks += '<span class="flag">Primary</span>'
        if not r.get("established"):
            marks += '<span class="flag bad">Never established</span>'
        settled = r.get("settled_payout_count") or 0
        body += (
            f'<tr><td class="mono">{_e(r.get("account_number"))}'
            f'<div class="note">{_e(r.get("ifsc"))} · '
            f'{_e(r.get("status"))}</div></td>'
            f'<td>{_e(V.added_via(r.get("added_via")))}'
            f'<div class="note">{_e(r.get("added_on") or "date not recorded")}</div></td>'
            f'<td>{_e(V.verified_by(r.get("verified_by")))}</td>'
            f'<td class="mono">{settled}'
            f'<div class="note">{"payouts" if settled != 1 else "payout"}</div></td>'
            f"<td>{marks}</td></tr>")
    body += "</tbody></table>"

    # The commonest case, and the panel used to stay silent about it: the
    # destination is none of these. Listing the known accounts without saying
    # so reads as though nothing is wrong, which is the opposite of the truth.
    if not any(r.get("is_destination") for r in rows):
        dest = (a.get("destination") or {}).get("account_number")
        body += ('<div class="step">This payment is going to '
                 f'<strong>{_e(dest or "an unresolved account")}</strong>, '
                 'which is not one of the accounts above. That is what the '
                 'destination check reports, and why the payment is held.</div>')

    weak = [r for r in rows if not r.get("established")]
    if weak:
        body += ('<div class="note">An account is <strong>established</strong> '
                 'once something outside email has confirmed it — onboarding '
                 'checks, a rupee from the account, a callback — or once it has '
                 'actually carried a payout. Being on file is not the same '
                 'thing: an account can be on file because one email asked for '
                 'it and nobody checked. The engine treats those as unconfirmed '
                 'rather than as wrong, so they hold rather than reject.</div>')
    body += "</div>"
    return body


def _case_history(actions) -> str:
    if not actions:
        return ""
    body = '<h2>Case history</h2><div class="card"><div class="hist">'
    for a in actions:
        label = casefile.ACTIONS.get(a.get("action"), (a.get("action"), ""))[0]
        body += (f'<div class="hwhen">{_when(a.get("at"))}</div>'
                 f'<div><strong>{_e(label)}</strong>'
                 f'<div class="note">recorded by {_e(a.get("actor"))}'
                 f'{" · " + _e(a.get("detail")) if a.get("detail") else ""}</div>'
                 + (f'<div class="reason">{_e(a.get("note"))}</div>'
                    if a.get("note") else "")
                 + '</div>')
    body += "</div></div>"
    return body


def render_case(a: Optional[Dict[str, Any]], case=None, actor: str = "",
                error: str = "") -> str:
    if a is None:
        body = _header("not found")
        body += ('<div class="empty">No decision recorded for that payout.'
                 '<br><a href="/">Back to decisions</a></div>')
        return _page("PayeeProof — not found", body)

    actions = list(case or [])
    actor = actor or casefile.OPERATORS[0][0]

    d = a.get("decision", {})
    dest = a.get("destination", {})
    doc = a.get("document", {})
    ext = a.get("extraction", {})
    sem = ext.get("semantic", {}) if ext.get("ok") else {}

    body = _header(f"payout {a.get('payout_id')}",
                   '<a href="/inbox">Inbox</a> · '
                   '<a href="/">← All decisions</a>')

    if error:
        body += f'<div class="refused"><strong>Refused.</strong> {_e(error)}</div>'

    # ── where the case stands, before any of the evidence ────────────
    summary = casefile.summary(actions, a.get("final_outcome"))
    if summary["state"] != "no_action":
        body += (f'<div class="card statebar">'
                 f'<span class="pill {STATE_CLASS.get(summary["state"], "muted")}">'
                 f'{_e(summary["label"])}</span>')
        if summary["next_step"]:
            body += f'<div class="reason">{_e(summary["next_step"])}</div>'
        if summary["verifiers"]:
            body += ('<div class="note">Verification recorded by '
                     f'{_e(", ".join(summary["verifiers"]))} — so the release '
                     'must come from somebody else.</div>')
        body += "</div>"

    rule_fired = d.get("rule_fired")
    body += ('<div class="card">'
             f'<div style="margin-bottom:10px">'
             f'{_outcome_pill(a.get("final_outcome", "?"))} {_recommendation(d)}'
             '</div>'
             f'<div><strong>{_e(V.rule(rule_fired))}</strong>'
             f'<div class="note mono">{_e(rule_fired)}</div></div>')
    step = V.next_step(rule_fired)
    if step:
        body += f'<div class="step">{_e(step)}</div>'
    body += (f'<div class="reason" title="{_e(d.get("reason"))}">'
             f'{_e(_humanise(d.get("reason")))}</div></div>')

    # ── what would release it, then the buttons that record it ───────
    ver = a.get("verification")
    demand = a.get("verification_demand") or {}
    named = None
    if ver or demand:
        ver = ver or {}
        body += '<h2>Verification — what would release this</h2><div class="card">'

        # THE CONTROL, and it was computed and never shown. "Prove you control
        # an account on file" lets an attacker use one they planted; naming the
        # account is the entire defence, so the operator has to see WHICH.
        #
        # Prefer what verification actually used; fall back to what it WOULD
        # demand. On the webhook path channel 2 is never attempted inline, so
        # without the fallback the answer is absent exactly where an operator
        # would act on it.
        named = ver.get("verification_account") or demand.get("account_number")
        basis = ver.get("verification_account_basis") or demand.get("basis")
        if named:
            body += ('<div class="demand">'
                     '<div class="note">Ask the supplier to send Rs 1 from '
                     'this account, and no other:</div>'
                     f'<div class="acct">{_e(named)}</div>'
                     f'<div class="note">chosen because {_e(basis)}</div>'
                     '</div>')
        elif (ver.get("outcome") == "CHANNEL_2_UNAVAILABLE"
              or demand.get("available") is False):
            body += ('<div class="demand unavailable">'
                     '<div class="acct">No account qualifies</div>'
                     f'<div class="note">{_e(basis)}</div>'
                     '<div class="note">This is not the same as the check '
                     'failing. Nothing can be asked for, so it escalates and '
                     'never falls back to the phone call.</div></div>')

        if ver.get("outcome"):
            body += ('<dl class="kv">'
                     f'<dt>Outcome</dt><dd>{_e(V.verification(ver.get("outcome")))}'
                     f'<div class="note mono">{_e(ver.get("outcome"))}</div></dd>'
                     f'<dt>Callback to</dt><dd>{_e(ver.get("contact_used"))}</dd>'
                     f'<dt>Number came from</dt><dd>{_e(ver.get("contact_source"))}'
                     ' — never a number in the request</dd>'
                     f'<dt>Attempts</dt><dd>{_e(ver.get("attempts"))}</dd>'
                     f'<dt>Escalated</dt><dd>{_e(ver.get("escalated"))}</dd>'
                     '</dl>'
                     f'<div class="reason">{_e(_humanise(ver.get("reason")))}</div>')

        # Segregation of duties, stated as a requirement AND enforced below.
        # A single "Approve" button quietly removes this.
        body += ('<div class="duties"><strong>Two people, not one.</strong> '
                 'Whoever records the verification outcome must not be whoever '
                 'releases the payment. A compromised or complicit AP clerk who '
                 'can do both approves their own request, and the control is '
                 'theatre. This is enforced when the button is pressed, not '
                 'only drawn on the screen.</div>')
        body += "</div>"

    body += _case_actions_panel(a.get("payout_id"), actions, actor, named,
                                a.get("final_outcome"))
    body += _case_history(actions)

    body += "<h2>What was checked</h2><div class=\"card\"><dl class=\"kv\">"
    # The friendly label with the raw identifier beside it — the same bargain
    # the rest of the page makes. The operator reads the sentence; the auditor
    # still gets the exact field the evidence came from.
    src_code = dest.get("source")
    ev_code = ext.get("evidence_source")
    for label, value in (
        ("Supplier", a.get("vendor_id")),
        ("Destination account", dest.get("account_number")),
        ("Destination came from",
         f"{V.source(src_code)} — the payout's own fund account, "
         f"never the email [{src_code}]"),
        ("Fund account", dest.get("fund_account_id")),
        ("Amount", f"Rs {a['amount_rupees']:,.2f}" if a.get("amount_rupees") else "not stated"),
        ("Change request", doc.get("document_id") or "none on file"),
        ("Correlated by", doc.get("correlation")),
        ("Evidence source", f"{V.source(ev_code)} [{ev_code}]"),
        ("Semantic reading",
         f"{sem.get('intent')} / {sem.get('action')} / {sem.get('scope')}"
         if sem else f"none — {ext.get('failure_reason') or 'no document'}"),
    ):
        body += f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>"
    body += "</dl></div>"

    body += _accounts_panel(a)

    for tier, label in (("tier1", "Identity checks — against your supplier records"),
                        ("tier2", "Circumstances — never decisive on their own")):
        rows = d.get(tier) or []
        if not rows:
            continue
        body += f"<h2>{_e(label)}</h2><div class=\"sig\">"
        body += "<div>result</div><div>check</div><div>finding</div>"
        body += _signal_rows_v2(rows)
        body += "</div>"

    body += "<h2>What this maps to at RazorpayX</h2><div class=\"card\">"
    for act in a.get("razorpay_actions", []):
        if act.get("method"):
            confirm = ('<span class="tag">needs human confirmation</span>'
                       if act.get("requires_human_confirmation") else "")
            body += (f'<div class="mono">{_e(act["method"])} '
                     f'{_e(act["endpoint"])} {confirm}'
                     f'<div class="note">{_e(act.get("effect"))}</div></div>')
        else:
            body += f'<div class="note">{_e(act.get("effect"))}</div>'
    body += ('<p class="note">Action plans. Nothing in this repository calls '
             'Razorpay.</p></div>')

    return _page(f"PayeeProof — {a.get('payout_id')}", body)
