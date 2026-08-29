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
from typing import Any, Dict, List, Optional

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
.note{color:var(--faint);font-size:12px;margin-top:6px}
.reason{color:var(--dim);font-size:12.5px;margin-top:4px;max-width:80ch}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim)}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


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
    return (f"<span class=\"pill {OUTCOME_CLASS.get(outcome, 'muted')}\">"
            f"{_e(outcome)}</span>")


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
                   + (f" · {rec} recommended for rejection" if rec else ""))
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


def render_case(a: Optional[Dict[str, Any]]) -> str:
    if a is None:
        body = _header("not found")
        body += ("<div class=\"empty\">No decision recorded for that payout."
                 "<br><a href=\"/\">Back to decisions</a></div>")
        return _page("PayeeProof — not found", body)

    d = a.get("decision", {})
    dest = a.get("destination", {})
    doc = a.get("document", {})
    ext = a.get("extraction", {})
    sem = ext.get("semantic", {}) if ext.get("ok") else {}

    body = _header(f"payout {a.get('payout_id')}",
                   "<a href=\"/\">← all decisions</a>")

    body += ("<div class=\"card\">"
             f"<div style=\"margin-bottom:10px\">{_outcome_pill(a.get('final_outcome','?'))} "
             f"{_recommendation(d)}"
             f"<span class=\"mono\" style=\"margin-left:8px\">{_e(d.get('rule_fired'))}</span></div>"
             f"<div class=\"reason\">{_e(d.get('reason'))}</div></div>")

    body += "<h2>What was checked</h2><div class=\"card\"><dl class=\"kv\">"
    for label, value in (
        ("Vendor", a.get("vendor_id")),
        ("Destination account", dest.get("account_number")),
        ("Destination came from", dest.get("source")),
        ("Fund account", dest.get("fund_account_id")),
        ("Amount", f"Rs {a['amount_rupees']:,.2f}" if a.get("amount_rupees") else "not stated"),
        ("Change request", doc.get("document_id") or "none on file"),
        ("Correlated by", doc.get("correlation")),
        ("Evidence source", ext.get("evidence_source")),
        ("Semantic reading",
         f"{sem.get('intent')} / {sem.get('action')} / {sem.get('scope')}"
         if sem else f"none — {ext.get('failure_reason') or 'no document'}"),
    ):
        body += f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>"
    body += "</dl></div>"

    for tier, label in (("tier1", "Tier 1 — identity, against the vendor master"),
                        ("tier2", "Tier 2 — contextual, never decisive alone")):
        rows = d.get(tier) or []
        if not rows:
            continue
        body += f"<h2>{_e(label)}</h2><div class=\"sig\">"
        body += "<div>result</div><div>signal</div><div>finding</div>"
        body += _signal_rows(rows)
        body += "</div>"

    ver = a.get("verification")
    if ver:
        body += ("<h2>Verification</h2><div class=\"card\"><dl class=\"kv\">"
                 f"<dt>Outcome</dt><dd>{_e(ver.get('outcome'))}</dd>"
                 f"<dt>Contact used</dt><dd>{_e(ver.get('contact_used'))}</dd>"
                 f"<dt>Contact source</dt><dd>{_e(ver.get('contact_source'))}</dd>"
                 f"<dt>Attempts</dt><dd>{_e(ver.get('attempts'))}</dd>"
                 "</dl>"
                 f"<div class=\"reason\">{_e(ver.get('reason'))}</div></div>")

    body += "<h2>What this maps to at RazorpayX</h2><div class=\"card\">"
    for act in a.get("razorpay_actions", []):
        if act.get("method"):
            confirm = ("<span class=\"tag\">needs human confirmation</span>"
                       if act.get("requires_human_confirmation") else "")
            body += (f"<div class=\"mono\">{_e(act['method'])} "
                     f"{_e(act['endpoint'])} {confirm}"
                     f"<div class=\"note\">{_e(act.get('effect'))}</div></div>")
        else:
            body += f"<div class=\"note\">{_e(act.get('effect'))}</div>"
    body += ("<p class=\"note\">Action plans. Nothing in this repository calls "
             "Razorpay.</p></div>")

    return _page(f"PayeeProof — {a.get('payout_id')}", body)
