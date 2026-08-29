"""
PayeeProof — case to email renderer.

Turns a feature row from generate_data.py into the message a finance team would
actually have received, so the real extractor can be measured end to end.

THE LEAKAGE PROBLEM THIS FILE IS BUILT AROUND
=============================================
The README records the project making this exact mistake once: ablation corpus
v1 scored the keyword baseline at 92.3% because the paraphrases were written
first and the trigger lists were written afterwards to match them. The exam was
authored to fit the student.

Rendering the eval corpus is the same trap one layer down. If these templates
contain the words the keyword baseline greps for, the extraction eval stops
measuring semantic understanding and starts measuring vocabulary overlap.

Three defences, all mechanical rather than aspirational:

  1. BANNED_VOCABULARY below is imported from the baseline's own trigger lists.
     Every rendered message is checked against it and a hit is a hard failure,
     not a warning. Adding a trigger word to a template breaks the build.
  2. No template names a label, a scenario, a rule, or a signal. The renderer
     never sees the words "fraud", "legit", "urgency" or "BLOCK".
  3. eval/extraction_eval.py re-runs the keyword baseline over the rendered
     corpus. If it scores well, the renderer recreated the v1 problem and the
     extraction numbers are void. That check is reported, not buried.

Meaning is carried by WHAT IS SAID, never by which keyword appears:

  the old account keeps a role      -> ADD        (it still settles X)
  the old account keeps nothing     -> REPLACE    (everything arrives here now)
  an unpaid invoice is referenced   -> OUTSTANDING_AND_FUTURE
  only forthcoming work is          -> FUTURE_ONLY
  nothing about this sender moves   -> PAYMENT_FOLLOWUP

DETERMINISM
===========
Each case is rendered from a seed derived from its own case_id, not from global
random state. Rendering one case never shifts what another case looks like, and
re-rendering any case at any time reproduces it byte for byte. Every rendered
case carries the renderer version and a SHA-256 of its text so an extraction
result can be tied to exactly the message that produced it.
"""

import hashlib
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List

RENDERER_VERSION = "2.0.0"

# ── The leakage guard ─────────────────────────────────────────────────
# Imported from the baseline rather than retyped, so the two can never drift.

def _baseline_vocabulary() -> List[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "eval"))
    import ablation  # noqa: E402
    return sorted({
        w.lower()
        for lst in (ablation.CHANGE_TRIGGERS, ablation.ADD_TRIGGERS,
                    ablation.REPLACE_TRIGGERS, ablation.SCOPE_FUTURE_ONLY,
                    ablation.SCOPE_BOTH)
        for w in lst
    })


BANNED_VOCABULARY = _baseline_vocabulary()

# Words that would leak the ground truth itself rather than the baseline's cues.
BANNED_LABELS = ["fraud", "legit", "scenario", "attacker", "victim", "mule",
                 "compromis", "suspicious", "urgency", "block", "step_up",
                 "allow", "rebrand", "sim swap", "sim-swap"]


class LeakageError(AssertionError):
    pass


def assert_no_leakage(text: str, case_id: str = "?") -> None:
    """
    A hit here is a hard failure. Softening this to a warning is how the v1
    methodology error happened, and the point of the check is that it cannot be
    ignored under deadline pressure.
    """
    low = text.lower()
    for word in BANNED_VOCABULARY:
        if word in low:
            raise LeakageError(
                f"{case_id}: rendered message contains the keyword baseline's "
                f"trigger {word!r}. The extraction eval would then be measuring "
                f"vocabulary overlap, not semantics. Reword the template.")
    for word in BANNED_LABELS:
        if word in low:
            raise LeakageError(
                f"{case_id}: rendered message contains ground-truth vocabulary "
                f"{word!r}. Templates must describe events, not labels.")


# ── Fragment pools ────────────────────────────────────────────────────
# Every one of these is checked against BANNED_VOCABULARY by the test suite.

GREETINGS = ["Hi Meera,", "Hello Meera,", "Hi there,", "Morning Meera,"]

SIGNOFFS = ["Thanks,", "Best regards,", "Regards,", "Many thanks,"]

SENDER_NAMES = ["Priya Nair", "Rohit Menon", "Anjali Rao", "Kabir Shah",
                "Deepa Iyer", "Vikram Sethi"]

# The old destination retains nothing -> REPLACE
REPLACE_FRAMING = [
    "Our treasury has consolidated everything into a single facility this "
    "quarter, and settlements now arrive at {acct} ({ifsc}).",
    "The facility you have on file was with our previous banking partner, whose "
    "relationship with us ended last week. Everything reaches {acct} ({ifsc}) "
    "from here.",
    "Following an internal reorganisation, our receivables now flow through one "
    "facility: {acct} / {ifsc}. The prior arrangement is wound down.",
    "We have restructured our banking. {acct} ({ifsc}) is where we receive "
    "everything now.",
]

# The old destination keeps a role -> ADD
ADD_FRAMING = [
    "We have set up a separate facility for the new business unit: {acct} "
    "({ifsc}). The existing arrangement continues to serve everything else.",
    "Our infrastructure division now banks separately, at {acct} ({ifsc}). "
    "Work billed by the other division settles exactly where it always has.",
    "Both facilities run in parallel on our end now. {acct} ({ifsc}) handles "
    "the new unit; everything already raised settles through the existing route.",
]

# Nothing about this sender's destination moves -> PAYMENT_FOLLOWUP
FOLLOWUP_FRAMING = [
    "Chasing INV-{inv}. Our records show {acct} ({ifsc}) as the receiving "
    "account, exactly as it has always been, and nothing has landed yet.",
    "Following up on INV-{inv} — could you confirm whether the transfer was "
    "released? Details on your file are {acct} / {ifsc}, exactly as before.",
    "INV-{inv} is still showing open our side. Payment details are as you have "
    "them, {acct} ({ifsc}). Just checking where it sits.",
]

# An unpaid invoice plus continuing work -> OUTSTANDING_AND_FUTURE
SCOPE_BOTH_FRAMING = [
    "INV-{inv} from October is still open on our ledger, and the retainer runs "
    "through March — both are handled under the arrangement above.",
    "That covers INV-{inv}, which remains unsettled, as well as the monthly "
    "billing that continues.",
]

# Only forthcoming work -> FUTURE_ONLY
SCOPE_FUTURE_FRAMING = [
    "INV-{inv} should still settle where it always has. Only work billed from "
    "next quarter onward comes to the facility above.",
    "Anything already raised, INV-{inv} included, settles as before. The new "
    "facility takes what is billed from April.",
]

TIME_PRESSURE = [
    "Month-end closing is tomorrow, so do prioritise this one.",
    "Our quarter cuts off on Friday and this is the last item open.",
    "The audit sign-off is Monday and this is holding it up.",
    "We are past terms on this one now, so anything you can do today helps.",
]

CHANNEL_REDIRECT = [
    "Please reply on this thread rather than the old chain.",
    "Do write back to me here — I am handling this account now rather than the "
    "shared inbox.",
    "Easiest to keep this thread going with me directly, rather than the "
    "shared inbox which is not monitored closely.",
]

GSTIN_PLAIN = ["Our GST registration is {gstin}."]
GSTIN_HEDGED = [
    "Our GST registration should be the same as before, {gstin}, though worth "
    "double-checking against the invoice copy.",
    "GST is {gstin} as far as I can see, but do confirm against your file.",
]

# A reply inside a conversation that is already real. The tail is quoted
# history: correct invoice numbers, a buyer who has already written back. It
# carries no cue about the destination at all — the point of the scenario is
# that there is nothing wrong with the CHANNEL, so a template that hinted
# otherwise would be answering the question the extractor is being asked.
QUOTED_TAIL = [
    "> Received, thank you. I will get INV-{inv} into the Friday run.\n"
    "> Meera",
    "> Noted on INV-{inv}. Our approver is back Tuesday, so it goes out then.\n"
    "> Meera",
    "> Thanks for the copies. INV-{inv} is queued behind the month close —\n"
    "> should clear this week.\n"
    "> Meera",
]

AMOUNT_LINE = [
    "The amount due is Rs {amount:,.0f}.",
    "Total on this one is Rs {amount:,.0f}.",
    "Value is Rs {amount:,.0f} inclusive.",
]


# ── Rendered case ─────────────────────────────────────────────────────

@dataclass
class RenderedCase:
    case_id:          str
    vendor_id:        str
    email:            str
    renderer_version: str
    render_seed:      int
    sha256:           str
    expected:         Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> Dict[str, Any]:
        row = {
            "case_id": self.case_id,
            "vendor_id": self.vendor_id,
            "renderer_version": self.renderer_version,
            "render_seed": self.render_seed,
            "sha256": self.sha256,
            "email": self.email,
        }
        row.update({f"expected_{k}": v for k, v in self.expected.items()})
        return row


def _seed_for(case_id: str) -> int:
    """
    Per-case, derived from the id itself. Global random state would make one
    case's rendering depend on how many were rendered before it, so a single
    case could not be reproduced in isolation.
    """
    return int(hashlib.sha256(case_id.encode()).hexdigest()[:8], 16)


def _semantics(row: Dict[str, str]) -> Dict[str, str]:
    """What a flawless extractor should report, derived from the narrative."""
    action = row["action_type"]
    if action == "NONE":
        return {"intent": "PAYMENT_FOLLOWUP", "action": "NONE", "scope": "NONE"}
    if action == "ADD":
        return {"intent": "BENEFICIARY_CHANGE", "action": "ADD_FUND_ACCOUNT",
                "scope": "FUTURE_ONLY"}
    return {"intent": "BENEFICIARY_CHANGE", "action": "REPLACE_PAYOUT_DESTINATION",
            "scope": "OUTSTANDING_AND_FUTURE"}


def render_case(row: Dict[str, str], vendor: Dict[str, str]) -> RenderedCase:
    """Deterministic for a given case_id and renderer version."""
    seed = _seed_for(row["case_id"])
    rng = random.Random(seed)

    sem = _semantics(row)
    inv = 4000 + (seed % 900)
    acct = row["proposed_account_number"]
    ifsc = row["proposed_ifsc"]
    amount = float(row["amount"])

    parts = [
        f"From: {rng.choice(['payments', 'accounts', 'billing', 'finance'])}"
        f"@{row['sender_domain']}",
        "To: accounts@clientcorp.in",
        f"Subject: {'Re: ' if row.get('is_reply') == 'True' else ''}INV-{inv}",
        "",
        rng.choice(GREETINGS),
        "",
    ]

    if sem["action"] == "NONE":
        parts.append(rng.choice(FOLLOWUP_FRAMING).format(acct=acct, ifsc=ifsc, inv=inv))
    elif sem["action"] == "ADD_FUND_ACCOUNT":
        parts.append(rng.choice(ADD_FRAMING).format(acct=acct, ifsc=ifsc))
        parts.append(rng.choice(SCOPE_FUTURE_FRAMING).format(inv=inv))
    else:
        parts.append(rng.choice(REPLACE_FRAMING).format(acct=acct, ifsc=ifsc))
        parts.append(rng.choice(SCOPE_BOTH_FRAMING).format(inv=inv))

    if row["proposed_gstin"]:
        pool = GSTIN_HEDGED if row["hedged_gstin"] == "True" else GSTIN_PLAIN
        parts.append(rng.choice(pool).format(gstin=row["proposed_gstin"]))

    parts.append(rng.choice(AMOUNT_LINE).format(amount=amount))

    if row["urgency_language"] == "True":
        parts.append(rng.choice(TIME_PRESSURE))
    if row["channel_manipulation"] == "True":
        parts.append(rng.choice(CHANNEL_REDIRECT))

    parts += ["", rng.choice(SIGNOFFS),
              f"{rng.choice(SENDER_NAMES)}", vendor["legal_name"]]

    if row.get("is_reply") == "True":
        parts += ["", rng.choice(QUOTED_TAIL).format(inv=inv)]

    email = "\n".join(p for p in parts if p is not None).strip()
    assert_no_leakage(email, row["case_id"])

    return RenderedCase(
        case_id=row["case_id"],
        vendor_id=row["vendor_id"],
        email=email,
        renderer_version=RENDERER_VERSION,
        render_seed=seed,
        sha256=hashlib.sha256(email.encode("utf-8")).hexdigest(),
        expected={
            **sem,
            # A follow-up PROPOSES nothing. The field is proposed_account_number,
            # so None is the correct reading of a message that merely restates
            # where payment has always gone. Expecting the number here scored
            # the extractor wrong for being right.
            "account_number": None if sem["action"] == "NONE" else acct,
            "ifsc": None if sem["action"] == "NONE" else ifsc,
            "gstin": row["proposed_gstin"],
            "sender_domain": row["sender_domain"],
            "amount": amount,
            "urgency": row["urgency_language"] == "True",
            "channel_manipulation": row["channel_manipulation"] == "True",
            "hedged_gstin": row["hedged_gstin"] == "True",
        },
    )


def render_split(split: str = "dev"):
    import csv
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "vendor_master.csv"), encoding="utf-8") as f:
        vendors = {r["vendor_id"]: r for r in csv.DictReader(f)}
    with open(os.path.join(here, f"cases_{split}.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [render_case(r, vendors[r["vendor_id"]]) for r in rows]


if __name__ == "__main__":
    split = sys.argv[1] if len(sys.argv) > 1 else "dev"
    cases = render_split(split)
    print(f"rendered {len(cases)} cases from cases_{split}.csv "
          f"(renderer {RENDERER_VERSION})")
    print(f"distinct messages: {len({c.sha256 for c in cases})}")
    print()
    print("=" * 72)
    print(cases[0].email)
    print("=" * 72)
