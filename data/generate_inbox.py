"""
BaseDrift — synthetic accounts-payable inbox.

WHY THIS IS A SEPARATE FILE FROM generate_data.py
=================================================
Because it must not touch the case corpus. Any change to generate_data.py voids
every cached extraction — ~2 h of API calls on a clean run, days against a free
tier — and the inbox does not need to change a single case. It READS the already
rendered messages and surrounds them with the traffic a real mailbox carries.

So: the change requests here ARE the corpus, byte for byte, and the noise is
new. Re-running this file costs nothing.

WHAT AN AP INBOX ACTUALLY LOOKS LIKE
====================================
Roughly 500 messages a day at a merchant doing 20,000 payouts, of which perhaps
40 concern where money goes and a handful of those are fraudulent. The noise is
not decoration — it is the thing triage exists to survive, and a triage eval
against a mailbox containing only change requests would measure nothing.

The noise is deliberately adversarial to the CHEAP stages:

  - Invoices and statements QUOTE ACCOUNT NUMBERS, because real ones do. A
    keyword pre-read that greps for "account" cannot separate them from a change
    request, which is the point: if the noise were keyword-separable the
    classifier stage would look far better than it is.
  - Some noise comes from real vendor domains, so vendor resolution alone does
    not decide.
  - Some comes from domains that resemble vendor domains but are ordinary
    business mail, so a lookalike match is not by itself a verdict.
  - Auto-replies carry real RFC 3834 headers rather than a marker column.

GROUND TRUTH
============
is_change_request describes the NARRATIVE — did this message ask for money to go
somewhere different — and true_vendor_id says who really sent it. Neither is a
copy of what any stage of triage computes. The v1 mistake this avoids is
encoding the policy's own answer in the data (see controls_existing_account in
NOTES.md V2.S); a dataset that already knows the verdict cannot measure it.
"""

import csv
import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render as R  # noqa: E402

SEED = 20260829
AS_OF_EPOCH = 1782777600.0        # 2026-06-30T00:00:00Z, matching generate_data

# Noise messages per genuine change request.
#
# WAS 12, WHICH MADE THE MAILBOX 7.7% BANK-CHANGE REQUESTS. No accounts-payable
# inbox looks like that. It also put fraud at 11.3% of everything triage routed
# for review, and a queue where one message in nine is an attack trains an
# operator to expect attacks — the opposite of the vigilance problem this system
# is built for, where fraud is rare enough to be forgotten about.
#
# At 40 the mailbox is ~2.4% change requests and fraud is ~1% of it. The review
# queue stays about the same SIZE, because it is dominated either way by
# ordinary mail that happens to quote an account number — which is the honest
# picture of what triage does, and the reason stage 3 exists at all.
NOISE_PER_CASE = 40


# ── Noise bodies. Several quote account numbers on purpose. ───────────

INVOICE = [
    "Please find invoice INV-{inv} attached, Rs {amt:,.0f}, payable within 30 days.\n"
    "Remittance details are unchanged: {acct} ({ifsc}).",
    "Invoice INV-{inv} for the March consignment, Rs {amt:,.0f}. Our banking\n"
    "details are as held on your file, {acct} / {ifsc}.",
    "Attaching INV-{inv}. Value Rs {amt:,.0f} inclusive of GST. Terms 45 days.",
]

STATEMENT = [
    "Statement of account as at month end. Two items open, INV-{inv} and\n"
    "INV-{inv2}, totalling Rs {amt:,.0f}. Settlement to {acct} as usual.",
    "Monthly reconciliation attached. Balance carried forward Rs {amt:,.0f}.",
]

CHASER = [
    "Just checking whether INV-{inv} made it into this week's run.",
    "Any update on INV-{inv}? It is showing 12 days past terms on our side.",
    "Gentle reminder about INV-{inv}, Rs {amt:,.0f}.",
]

LOGISTICS = [
    "Delivery for order PO-{inv} is scheduled Thursday between 9 and 12.\n"
    "Driver will call ahead.",
    "The consignment against PO-{inv} left our Pune depot this morning.",
    "Please confirm a receiving window for PO-{inv} next week.",
]

INTERNAL = [
    "Meera — the Q1 vendor spend pack is on the shared drive, sheet 3 has the\n"
    "top twenty by value.",
    "Reminder: purchase approvals above Rs 5,00,000 need a second signature\n"
    "from this month.",
    "Team, the finance system is down for patching on Saturday 0600-1000.",
]

AUTOREPLY = [
    "I am away from the office until the 14th with limited access to email.\n"
    "For anything urgent please contact the desk.",
    "Your message has been received and assigned reference SR-{inv}. A member\n"
    "of the team will respond within two working days.",
]

NOREPLY = [
    "Your payment advice for reference PA-{inv} is available in the portal.",
    "This is an automated notification. Statement {inv} has been generated.",
]

SPAM = [
    "Unlock working capital in 24 hours. Rates from 0.9% per month. Reply STOP\n"
    "to opt out.",
    "Your business qualifies for a pre-approved facility. Limited window.",
    "Conference on treasury automation, early-bird pricing closes Friday.",
]


def _mid(rng):
    return f"<{rng.getrandbits(64):016x}@mail.example>"


def _acct(rng):
    return "".join(rng.choices("0123456789", k=12))


def _ifsc(rng):
    return rng.choice(["HDFC", "ICIC", "SBIN", "AXIS", "KKBK"]) + "0" + \
        "".join(rng.choices("0123456789", k=6))


def _noise_message(rng, vendors, vendor_ids, seq, primary=None):
    """
    One message that is NOT a beneficiary change request.

    The sender is picked before the body, so noise from a real vendor domain is
    common — vendor resolution passing is not evidence of anything on its own.
    """
    inv = 4000 + rng.randrange(900)
    amt = rng.choice([15000, 28000, 45000, 82000, 150000, 310000]) * \
        rng.uniform(0.8, 1.2)
    headers = {}
    kind = rng.choices(
        ["invoice", "statement", "chaser", "logistics", "internal",
         "autoreply", "noreply", "spam"],
        weights=[26, 10, 18, 12, 12, 6, 6, 10], k=1)[0]

    vendor = vendors[rng.choice(vendor_ids)]

    if kind == "internal":
        sender = rng.choice(["ops", "controller", "fp.a", "procurement"]) + \
            "@clientcorp.in"
        body = rng.choice(INTERNAL)
        subject = "internal"
    elif kind == "spam":
        sender = rng.choice(["offers@quickfundsindia.co", "hello@treasurysummit.io",
                             "growth@capitaledge-partners.com"])
        body = rng.choice(SPAM)
        subject = "opportunity"
    elif kind == "autoreply":
        sender = rng.choice(["priya", "rohit", "anjali"]) + "@" + vendor["known_domain"]
        body = rng.choice(AUTOREPLY).format(inv=inv)
        subject = "Out of office"
        headers = rng.choice([
            {"Auto-Submitted": "auto-replied"},
            {"X-Autoreply": "yes"},
            {"Precedence": "bulk"},
        ])
    elif kind == "noreply":
        sender = rng.choice(["no-reply", "noreply", "do-not-reply"]) + \
            "@" + vendor["known_domain"]
        body = rng.choice(NOREPLY).format(inv=inv)
        subject = f"Notification {inv}"
    else:
        sender = rng.choice(["accounts", "billing", "payments", "dispatch"]) + \
            "@" + vendor["known_domain"]
        pool = {"invoice": INVOICE, "statement": STATEMENT,
                "chaser": CHASER, "logistics": LOGISTICS}[kind]
        # THE ACCOUNT A ROUTINE MESSAGE QUOTES IS THE ONE ON FILE.
        #
        # These templates say "unchanged", "as held on your file" and "as
        # usual", and used to fill that in with a freshly random number. The
        # text and the data contradicted each other, and the consequence was
        # not cosmetic: a legitimate supplier's mailbox history named twenty-odd
        # DISTINCT accounts, only 4.9% of which were on file. Destination churn
        # became unmeasurable, because every sender looked like they changed
        # account on every message. See NOTES.md V2.E.
        #
        # The rng draws still happen so the stream is unchanged and only the
        # account numbers move — shifting the stream would resample every
        # message and silently void the triage numbers already recorded.
        template = rng.choice(pool)          # drawn FIRST, as it always was
        fallback_acct, fallback_ifsc = _acct(rng), _ifsc(rng)
        on_file = (primary or {}).get(vendor["vendor_id"])
        body = template.format(
            inv=inv, inv2=inv + 7, amt=amt,
            acct=on_file[0] if on_file else fallback_acct,
            ifsc=on_file[1] if on_file else fallback_ifsc)
        subject = {"invoice": f"INV-{inv}", "statement": "Statement of account",
                   "chaser": f"INV-{inv}", "logistics": f"PO-{inv}"}[kind]

    return {
        "message_id": _mid(rng),
        "thread_id": f"THRN{seq:05d}",
        "from_addr": sender,
        "to_addr": "accounts@clientcorp.in",
        "subject": subject,
        "body": body,
        "received_at": round(AS_OF_EPOCH - rng.uniform(0, 90 * 86400), 3),
        "headers": json.dumps(headers),
        "in_reply_to": "",
        "case_id": "",
        "true_vendor_id": "" if kind in ("internal", "spam") else vendor["vendor_id"],
        "is_change_request": False,
        "noise_kind": kind,
    }


def build_inbox(split="dev"):
    rng = random.Random(SEED + (0 if split == "dev" else 1))

    with open(os.path.join(HERE, "vendor_master.csv"), encoding="utf-8") as f:
        vendors = {r["vendor_id"]: r for r in csv.DictReader(f)}
    with open(os.path.join(HERE, f"cases_{split}.csv"), encoding="utf-8") as f:
        rows = {r["case_id"]: r for r in csv.DictReader(f)}

    # {vendor_id: (account, ifsc)} for the account each vendor actually settles
    # to, so routine mail quotes it rather than a random number.
    primary = {}
    with open(os.path.join(HERE, "vendor_accounts.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["is_primary"] == "True":
                primary[r["vendor_id"]] = (r["account_number"], r["ifsc"])

    messages = []

    # The change requests: the rendered corpus, unchanged.
    for rendered in R.render_split(split):
        row = rows[rendered.case_id]
        lines = rendered.email.split("\n")
        sender = lines[0].replace("From:", "").strip()
        subject = lines[2].replace("Subject:", "").strip()
        body = "\n".join(lines[4:]).strip()
        messages.append({
            "message_id": f"<{rendered.sha256[:16]}@vendor.mail>",
            "thread_id": row["thread_id"],
            "from_addr": sender,
            "to_addr": "accounts@clientcorp.in",
            "subject": subject,
            "body": body,
            "received_at": round(AS_OF_EPOCH - rng.uniform(0, 90 * 86400), 3),
            "headers": json.dumps({}),
            "in_reply_to": (f"<prior-{row['thread_id']}@vendor.mail>"
                            if row["is_reply"] == "True" else ""),
            "case_id": rendered.case_id,
            "true_vendor_id": row["vendor_id"],
            "is_change_request": True,
            "noise_kind": "",
        })

    vendor_ids = sorted(vendors)
    for i in range(len(messages) * NOISE_PER_CASE):
        messages.append(_noise_message(rng, vendors, vendor_ids, i, primary))

    rng.shuffle(messages)
    return messages


def main():
    for split in ("dev", "holdout"):
        messages = build_inbox(split)
        path = os.path.join(HERE, f"inbox_{split}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            # LF, for the same reason as generate_data.write_csv.
            w = csv.DictWriter(f, fieldnames=list(messages[0].keys()),
                               lineterminator="\n")
            w.writeheader()
            w.writerows(messages)

        changes = sum(1 for m in messages if m["is_change_request"])
        kinds = {}
        for m in messages:
            if m["noise_kind"]:
                kinds[m["noise_kind"]] = kinds.get(m["noise_kind"], 0) + 1
        print(f"inbox_{split}.csv: {len(messages)} messages, "
              f"{changes} change requests ({changes / len(messages):.1%})")
        print(f"   noise: {dict(sorted(kinds.items()))}")

        # The property that makes the corpus worth evaluating against: a keyword
        # pre-read must NOT separate the classes on its own.
        sys.path.insert(0, os.path.join(HERE, "..", "src"))
        import triage as T
        hits = sum(1 for m in messages
                   if not m["is_change_request"]
                   and T.looks_like_it_touches_money(
                       T.Message(message_id="x", from_addr=m["from_addr"],
                                 subject=m["subject"], body=m["body"])))
        noise = len(messages) - changes
        print(f"   {hits} of {noise} noise messages ({hits / noise:.1%}) mention a "
              f"payment destination — the keyword pre-read cannot decide alone")
        print()


if __name__ == "__main__":
    main()
