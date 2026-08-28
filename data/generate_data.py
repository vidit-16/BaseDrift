"""
PayeeProof — synthetic data generator.

Three outputs:
  vendor_master.csv    — the "trusted source" every request gets checked against
  cases_dev.csv        — 70% of cases, for tuning thresholds/rules
  cases_holdout.csv    — 30% of cases, scored exactly once at the end

METHODOLOGY — the rule this file exists to enforce
--------------------------------------------------
The label (fraud / legit) comes from an authored NARRATIVE, and feature values
are generated to be consistent with that narrative. Nothing here checks "does
the name match the vendor master" and then calls that fraud — that would leak
detector logic into ground truth. Each scenario_* function is a short story
first, features second.

The scenarios below are derived from documented BEC patterns (see the FBI IC3
reference in README.md), NOT from what the current rule table happens to catch.
Several were added specifically because they are expected to expose failures:
legit_rebrand should produce false BLOCKs under the present R4, and
fraud_sim_swap defeats the callback entirely. If a scenario only ever confirms
that the rules work, it is not earning its place in the file.

THE SECOND VERIFICATION CHANNEL
------------------------------
`controls_existing_account` records whether the requester can still move money
OUT of the account already on file — what a Reverse Penny Drop pointed at the
OLD account would establish.

It is deliberately NOT a copy of the label. Attackers never have it: taking over
a mailbox, or even a phone, does not give you the victim's bank account, and
redirecting money away from that account is the entire point of the attack. But
legitimate vendors do not always have it either — a real bank switch may mean
the old facility is already closed. Those cases exist in the data on purpose,
because a channel that only ever appears in cases it resolves has not been
tested against anything.

WITHIN-SCENARIO VARIANCE
------------------------
An earlier version of this generator emitted ONE fixed feature vector per
scenario, so 800 cases were four distinct tests wearing different random digits
and nothing could be tuned against them — R4's threshold gave identical results
at 1 and at 5. Every scenario here randomises the incidental features (urgency,
channel manipulation, hedging, amount deviation, FAV availability) around a
fixed narrative core, so the cases within a scenario genuinely differ.
"""

import csv
import hashlib
import os
import random

random.seed(42)  # reproducible dataset

# ---------------------------------------------------------------------------
# Reference pools for realistic-looking synthetic values
# ---------------------------------------------------------------------------

BUSINESS_CORE = [
    "Shreeji", "Vishal", "National", "Om", "Sai", "Krishna", "Balaji",
    "Everest", "Sunrise", "Metro", "Prime", "Falcon", "Ganga", "Comet",
    "Orbit", "Nova", "Zenith", "Anand", "Suraksha", "Vertex",
]
BUSINESS_SUFFIX = [
    "Traders", "Enterprises", "Industries", "Pvt Ltd", "Corp", "Solutions",
    "Logistics", "Textiles", "Packaging", "Distributors", "Agro", "Systems",
]
CITY_STATE_CODE = {
    "Bengaluru": "29", "Mumbai": "27", "Delhi": "07", "Chennai": "33",
    "Hyderabad": "36", "Pune": "27", "Ahmedabad": "24", "Kolkata": "19",
}
BANK_IFSC_PREFIX = ["HDFC", "ICIC", "SBIN", "AXIS", "KKBK", "PUNB", "UTIB"]
LOOKALIKE_SWAPS = [("o", "0"), ("l", "1"), ("i", "1"), ("a", "@"), ("e", "3")]
REBRAND_SUFFIX = ["group", "global", "india", "holdings"]


def _gstin(state_code):
    pan_like = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=5))
    pan_like += "".join(random.choices("0123456789", k=4))
    pan_like += random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return f"{state_code}{pan_like}1Z{random.choice('0123456789')}"


def _pan_from_gstin(gstin):
    return gstin[2:12]


def _phone():
    return "9" + "".join(random.choices("0123456789", k=9))


def _account_number():
    return "".join(random.choices("0123456789", k=12))


def _ifsc():
    return random.choice(BANK_IFSC_PREFIX) + "0" + "".join(
        random.choices("0123456789", k=6)
    )


def _domain(legal_name):
    slug = legal_name.lower().replace(" ", "").replace(".", "")[:14]
    return f"{slug}.com"


def _lookalike_domain(domain):
    for a, b in random.sample(LOOKALIKE_SWAPS, k=1):
        if a in domain:
            return domain.replace(a, b, 1)
    return domain.replace(".com", "-billing.com")  # fallback swap


def _rebranded_domain(domain):
    """A legitimately different domain — acquisition, rename, group consolidation."""
    return domain.replace(".com", f"{random.choice(REBRAND_SUFFIX)}.com")


def _slightly_altered_gstin(gstin):
    """
    Alter one character of the PAN-like segment, guaranteed to differ.

    Positions 7-10 already hold digits, so a naive random.choice() can select
    the character that is already there and return the GSTIN UNCHANGED. That
    silently produced fraud_easy cases whose GSTIN matches the vendor master
    (~4% of them), which would later surface as unexplained eval misses.
    Resample until it actually differs.
    """
    pos = random.randint(2, 11)
    chars = list(gstin)
    original = chars[pos]
    replacement = original
    while replacement == original:
        replacement = random.choice("0123456789")
    chars[pos] = replacement
    return "".join(chars)


def _stable_bool(case_id: str, field: str, probability: float) -> bool:
    """
    A per-case coin flip that does NOT consume the global RNG stream.

    Adding a random.random() call inside a scenario shifts every subsequent
    draw, so every later case gets different features, so every rendered email
    changes, so the entire extraction cache misses — 157 already-measured cases
    became 3 the first time this field was added the obvious way. Deriving the
    value from the case id leaves the stream, and therefore every existing
    measurement, exactly where it was.
    """
    h = hashlib.sha256(f"{case_id}:{field}".encode()).hexdigest()
    return (int(h[:8], 16) % 10_000) / 10_000.0 < probability


def _amount_near(base, spread=0.10):
    """An amount that varies around the vendor's baseline rather than equalling it."""
    return round(base * random.uniform(1 - spread, 1 + spread), 2)


def _fav(active_p=0.94, available_p=0.93):
    """
    FAV is a live external dependency, not a guarantee. It goes down, and it
    returns non-active statuses. Neither was represented before, so two Tier 1
    branches had zero dataset coverage.

    Returns (account_status, name_available).
    """
    r = random.random()
    status = "active" if r < active_p else ("unknown" if r < active_p + 0.04 else "inactive")
    return status, random.random() < available_p


# ---------------------------------------------------------------------------
# Vendor master — the trusted record every request is checked against
# ---------------------------------------------------------------------------

def generate_vendor_master(n=75):
    vendors = []
    for i in range(n):
        city = random.choice(list(CITY_STATE_CODE.keys()))
        legal_name = f"{random.choice(BUSINESS_CORE)} {random.choice(BUSINESS_SUFFIX)}"
        gstin = _gstin(CITY_STATE_CODE[city])
        vendors.append({
            "vendor_id": f"VEND{i:04d}",
            "legal_name": legal_name,
            "gstin": gstin,
            "pan": _pan_from_gstin(gstin),
            "known_domain": _domain(legal_name),
            "known_phone": _phone(),
            "known_account_number": _account_number(),
            "known_ifsc": _ifsc(),
            "city": city,
            "avg_payout_amount": random.choice([15000, 28000, 45000, 82000, 150000, 310000]),
        })
    return vendors


# ---------------------------------------------------------------------------
# Scenario narratives — label comes from the story, features follow from it
# ---------------------------------------------------------------------------

def _base(vendor, case_id, scenario_type, label):
    """Fields every scenario shares; each story then overrides what it changes."""
    status, name_ok = _fav()
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": label,
        "scenario_type": scenario_type,
        "action_type": "REPLACE",
        "sender_domain": vendor["known_domain"],
        "sender_phone_used": vendor["known_phone"],
        "proposed_gstin": vendor["gstin"],
        "proposed_account_number": vendor["known_account_number"],
        "proposed_ifsc": vendor["known_ifsc"],
        "registered_name_returned": vendor["legal_name"],
        "name_match_score": 100,
        "fav_account_status": status,
        "fav_name_available": name_ok,
        "urgency_language": False,
        "channel_manipulation": False,
        "hedged_gstin": False,
        "amount": vendor["avg_payout_amount"],
        "near_duplicate_invoice": False,
        "split_below_threshold": False,
        "callback_reaches_known_contact": True,
        # Can the requester still move money OUT of the account already on
        # file? Reverse Penny Drop pointed at the OLD account asks exactly
        # that. It is the one thing a mail-and-phone attacker cannot do —
        # redirecting away from that account is the whole point of the attack.
        "controls_existing_account": True,
    }


# ── Fraud ─────────────────────────────────────────────────────────────

def scenario_fraud_easy(vendor, case_id, ctx):
    """Attacker compromises vendor's email, uses a lookalike domain, sends an
    invoice with a materially altered GSTIN and a brand-new account under a name
    that doesn't match the vendor master at all. The loud, unsubtle version."""
    c = _base(vendor, case_id, "fraud_easy", "fraud")
    c.update({
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),
        "proposed_gstin": _slightly_altered_gstin(vendor["gstin"]),
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "registered_name_returned": f"{random.choice(BUSINESS_CORE)} "
                                    f"{random.choice(BUSINESS_SUFFIX)}",
        "name_match_score": random.randint(10, 45),
        "urgency_language": random.random() < 0.85,
        "channel_manipulation": random.random() < 0.55,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.20),
        "callback_reaches_known_contact": False,
        "controls_existing_account": False,
    })
    return c


def scenario_fraud_hard(vendor, case_id, ctx):
    """Attacker did their homework — correct GSTIN, domain off by one character,
    real vendor name registered on an account they control. Amount split just
    under a plausible approval threshold. Only the destination and a faint
    urgency cue give it away."""
    c = _base(vendor, case_id, "fraud_hard", "fraud")
    c.update({
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(85, 100),
        "urgency_language": random.random() < 0.80,
        "channel_manipulation": random.random() < 0.50,
        "hedged_gstin": random.random() < 0.30,
        "amount": round(vendor["avg_payout_amount"] * random.uniform(0.93, 0.99), 2),
        "near_duplicate_invoice": random.random() < 0.70,
        "split_below_threshold": random.random() < 0.70,
        "callback_reaches_known_contact": False,
        "controls_existing_account": False,
    })
    return c


def scenario_fraud_compromised(vendor, case_id, ctx):
    """
    The attacker owns the vendor's actual mailbox. Nothing about the CHANNEL is
    wrong — the mail genuinely comes from the vendor's domain, quoting their real
    GSTIN, in a thread the buyer already trusts. Only the destination is new.

    This is the pattern that defeats domain-reputation controls, and the current
    dataset had no representation of it: every fraud case carried a lookalike
    domain, so a rule keyed on domain mismatch looked far better than it is.
    """
    c = _base(vendor, case_id, "fraud_compromised", "fraud")
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(88, 100),
        # Patient attacker: often no urgency tell at all.
        "urgency_language": random.random() < 0.35,
        "channel_manipulation": random.random() < 0.40,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.08),
        "callback_reaches_known_contact": False,
        "controls_existing_account": False,
    })
    return c


def scenario_fraud_mule(vendor, case_id, ctx):
    """
    One mule account collecting from several victims at once. The destination is
    an account already on file under a DIFFERENT vendor in the buyer's own master.

    RazorpayX permits an account under multiple contacts, which is legitimate for
    corporate groups — and is also exactly how one attacker redirects several
    vendors to one destination. The cross-contact FAIL branch had zero coverage.
    """
    c = _base(vendor, case_id, "fraud_mule", "fraud")
    others = [v for v in ctx["vendors"] if v["vendor_id"] != vendor["vendor_id"]]
    mule = random.choice(others)["known_account_number"]
    c.update({
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),
        "proposed_account_number": mule,
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(70, 100),
        "urgency_language": random.random() < 0.60,
        "channel_manipulation": random.random() < 0.45,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.15),
        "callback_reaches_known_contact": False,
        "controls_existing_account": False,
    })
    return c


def scenario_fraud_sim_swap(vendor, case_id, ctx):
    """
    The attacker also controls the phone number on file — SIM swap, ported
    number, or an insider at the vendor. The callback reaches someone who
    confirms the change.

    This is the case the entire callback mechanism cannot survive, and it is the
    only place where the RULES have to carry the decision alone. Without it,
    callback_reaches_known_contact correlates perfectly with the label and a
    pipeline running no rules scores 100%.
    """
    c = _base(vendor, case_id, "fraud_sim_swap", "fraud")
    c.update({
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(80, 100),
        "urgency_language": random.random() < 0.70,
        "channel_manipulation": random.random() < 0.50,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.15),
        "callback_reaches_known_contact": True,   # the callback is defeated
        # ...but the attacker still cannot send from the vendor's own account.
        # This is the scenario the second channel exists for.
        "controls_existing_account": False,
    })
    return c


# ── Legitimate ────────────────────────────────────────────────────────

def scenario_legit_easy(vendor, case_id, ctx):
    """Routine payout to a long-standing, unchanged account. Nothing should trip."""
    c = _base(vendor, case_id, "legit_easy", "legit")
    c.update({
        "action_type": "NONE",
        "amount": _amount_near(vendor["avg_payout_amount"], 0.06),
    })
    return c


def scenario_legit_hard(vendor, case_id, ctx):
    """Vendor genuinely switched banks and is genuinely in a hurry about being
    paid — real urgency, but the account is legitimately theirs and they answer
    on their known number. The false-positive canary.

    A genuine bank switch sometimes means the old account is already closed, so
    the vendor cannot send from it. That is the honest failure case for the
    second channel and it must be represented, not assumed away: a control whose
    dataset only contains cases it handles is not being tested.
    """
    c = _base(vendor, case_id, "legit_hard", "legit")
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "controls_existing_account": _stable_bool(case_id, "rpd", 0.75),
        "urgency_language": random.random() < 0.85,
        "hedged_gstin": random.random() < 0.25,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.10),
    })
    return c


def scenario_legit_rebrand(vendor, case_id, ctx):
    """
    The vendor was acquired. New parent company, new email domain, new banking
    arrangement, and a finance team chasing a quarter-end cutover — so the mail
    arrives from an unfamiliar domain, asking to move the destination, urgently.

    Every contextual signal a BEC detector keys on fires at once, and the request
    is completely genuine. Included precisely because the current R4 is expected
    to reject it.
    """
    c = _base(vendor, case_id, "legit_rebrand", "legit")
    c.update({
        "sender_domain": _rebranded_domain(vendor["known_domain"]),
        # An acquisition often consolidates banking, so the old facility may
        # already be gone.
        "controls_existing_account": _stable_bool(case_id, "rpd", 0.65),
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "urgency_language": random.random() < 0.75,
        "channel_manipulation": random.random() < 0.50,   # "use my new address"
        "hedged_gstin": random.random() < 0.30,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.12),
    })
    return c


def scenario_legit_add_account(vendor, case_id, ctx):
    """
    Vendor opens a second facility for a new business unit and asks for future
    invoices from that unit only. The existing account keeps settling everything
    already raised.

    RazorpayX permits multiple fund accounts per contact and the README argues
    ADD is materially lower risk than REPLACE — but no scenario exercised ADD,
    so that distinction was asserted and never measured.
    """
    c = _base(vendor, case_id, "legit_add_account", "legit")
    c.update({
        "action_type": "ADD",
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "urgency_language": random.random() < 0.30,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.15),
    })
    return c


def scenario_legit_unreachable(vendor, case_id, ctx):
    """
    Genuine bank change from a genuine vendor — who is then unreachable. Wrong
    number on file, festival shutdown, whoever owns the phone has left.

    The callback fails through no fault of the request. The correct handling is a
    held payout escalated to a human, NOT a rejection: this scenario exists to
    make the distinction between "held" and "blocked" measurable, since only one
    of those is a customer-facing failure.
    """
    c = _base(vendor, case_id, "legit_unreachable", "legit")
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "urgency_language": random.random() < 0.40,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.10),
        "callback_reaches_known_contact": False,
        # Nobody answers the phone, but the vendor still banks where they always
        # have. This is the scenario the second channel should RESCUE: a genuine
        # request currently held for want of a phone call.
        "controls_existing_account": True,
    })
    return c


SCENARIO_FUNCS = {
    "fraud_easy": scenario_fraud_easy,
    "fraud_hard": scenario_fraud_hard,
    "fraud_compromised": scenario_fraud_compromised,
    "fraud_mule": scenario_fraud_mule,
    "fraud_sim_swap": scenario_fraud_sim_swap,
    "legit_easy": scenario_legit_easy,
    "legit_hard": scenario_legit_hard,
    "legit_rebrand": scenario_legit_rebrand,
    "legit_add_account": scenario_legit_add_account,
    "legit_unreachable": scenario_legit_unreachable,
}

# Fraud is oversampled relative to real-world base rates, for statistical power.
# Stated explicitly here and stated explicitly in the writeup too. Nothing in
# these weights is derived from what the rules currently catch — the hard and
# adversarial scenarios are weighted UP precisely because they are where the
# rules are expected to struggle.
SCENARIO_WEIGHTS = {
    "fraud_easy":         0.09,
    "fraud_hard":         0.12,
    "fraud_compromised":  0.11,
    "fraud_mule":         0.06,
    "fraud_sim_swap":     0.06,
    "legit_easy":         0.20,
    "legit_hard":         0.14,
    "legit_rebrand":      0.10,
    "legit_add_account":  0.07,
    "legit_unreachable":  0.05,
}


def generate_cases(vendors, n=400):
    types = list(SCENARIO_WEIGHTS.keys())
    weights = list(SCENARIO_WEIGHTS.values())
    ctx = {"vendors": vendors}
    cases = []
    for i in range(n):
        vendor = random.choice(vendors)
        scenario_type = random.choices(types, weights=weights, k=1)[0]
        cases.append(SCENARIO_FUNCS[scenario_type](vendor, f"CASE{i:05d}", ctx))
    return cases


def split_dev_holdout(cases, holdout_frac=0.30):
    # Stratified by scenario_type so both splits carry every narrative type
    by_type = {}
    for c in cases:
        by_type.setdefault(c["scenario_type"], []).append(c)

    dev, holdout = [], []
    for scenario_type, group in by_type.items():
        random.shuffle(group)
        cut = int(len(group) * (1 - holdout_frac))
        dev.extend(group[:cut])
        holdout.extend(group[cut:])

    random.shuffle(dev)
    random.shuffle(holdout)
    return dev, holdout


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    # Write beside this file, not into the caller's cwd — pipeline.load_vendors()
    # looks for repo_root/data/vendor_master.csv.
    here = os.path.dirname(os.path.abspath(__file__))

    vendors = generate_vendor_master(n=120)
    write_csv(os.path.join(here, "vendor_master.csv"), vendors)

    cases = generate_cases(vendors, n=800)
    dev, holdout = split_dev_holdout(cases, holdout_frac=0.30)
    write_csv(os.path.join(here, "cases_dev.csv"), dev)
    write_csv(os.path.join(here, "cases_holdout.csv"), holdout)

    def summarize(name, rows):
        fraud = sum(1 for r in rows if r["label"] == "fraud")
        print(f"{name}: {len(rows)} cases, {fraud} fraud / {len(rows) - fraud} legit")
        for t in SCENARIO_WEIGHTS:
            count = sum(1 for r in rows if r["scenario_type"] == t)
            print(f"   {t:20s} {count}")

    print(f"vendor_master.csv: {len(vendors)} vendors\n")
    summarize("cases_dev.csv", dev)
    print()
    summarize("cases_holdout.csv", holdout)


if __name__ == "__main__":
    main()
