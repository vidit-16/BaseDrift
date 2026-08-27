"""
PayeeProof — synthetic data generator.

Three outputs:
  vendor_master.csv   — the "trusted source" every request gets checked against
  cases_dev.csv        — 70% of cases, for tuning thresholds/rules
  cases_holdout.csv    — 30% of cases, touched exactly once at the end

Core rule this generator exists to enforce: the label (fraud / legit) comes
from an authored NARRATIVE, and feature values are generated to be consistent
with that narrative. Nothing here checks "does name match vendor master" and
then calls that fraud — that would be leaking detector logic into ground
truth. Each scenario_* function is a short story first, features second.
"""

import csv
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

def scenario_fraud_easy(vendor, case_id):
    """Attacker compromises vendor's email, uses a lookalike domain, sends
    invoice with a materially altered GSTIN and a brand-new account under
    a name that doesn't match the vendor master at all."""
    fake_name = f"{random.choice(BUSINESS_CORE)} {random.choice(BUSINESS_SUFFIX)}"
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": "fraud",
        "scenario_type": "fraud_easy",
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),  # not the vendor's known number
        "proposed_gstin": _slightly_altered_gstin(vendor["gstin"]),
        "proposed_account_number": _account_number(),  # new, unseen account
        "proposed_ifsc": _ifsc(),
        "registered_name_returned": fake_name,  # FAV would return this
        "name_match_score": random.randint(10, 45),
        "urgency_language": True,
        "amount": vendor["avg_payout_amount"],
        "near_duplicate_invoice": False,
        "split_below_threshold": False,
        "callback_reaches_known_contact": False,  # attacker doesn't have real number
    }


def scenario_fraud_hard(vendor, case_id):
    """Attacker did their homework — correct GSTIN and a domain that's off
    by one character. Only the destination account and a faint urgency
    cue give it away. Amount is also split just under a plausible approval
    threshold to avoid scrutiny."""
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": "fraud",
        "scenario_type": "fraud_hard",
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),
        "proposed_gstin": vendor["gstin"],  # correct — attacker copied it
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "registered_name_returned": vendor["legal_name"],  # attacker used real name on a new account they control
        "name_match_score": random.randint(85, 100),  # looks clean on name alone
        "urgency_language": True,
        "amount": round(vendor["avg_payout_amount"] * 0.97, 2),  # just under typical threshold
        "near_duplicate_invoice": True,
        "split_below_threshold": True,
        "callback_reaches_known_contact": False,
    }


def scenario_legit_easy(vendor, case_id):
    """Routine, unremarkable payout to a vendor's long-standing, unchanged
    account. Nothing about this should trip anything."""
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": "legit",
        "scenario_type": "legit_easy",
        "sender_domain": vendor["known_domain"],
        "sender_phone_used": vendor["known_phone"],
        "proposed_gstin": vendor["gstin"],
        "proposed_account_number": vendor["known_account_number"],
        "proposed_ifsc": vendor["known_ifsc"],
        "registered_name_returned": vendor["legal_name"],
        "name_match_score": 100,
        "urgency_language": False,
        "amount": vendor["avg_payout_amount"],
        "near_duplicate_invoice": False,
        "split_below_threshold": False,
        "callback_reaches_known_contact": True,
    }


def scenario_legit_hard(vendor, case_id):
    """Vendor genuinely switched banks and is genuinely in a hurry about
    getting paid — real urgency, but the account is legitimately theirs and
    they answer on their known number when called back."""
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": "legit",
        "scenario_type": "legit_hard",
        "sender_domain": vendor["known_domain"],
        "sender_phone_used": vendor["known_phone"],
        "proposed_gstin": vendor["gstin"],
        "proposed_account_number": _account_number(),  # genuinely new account
        "proposed_ifsc": _ifsc(),
        "registered_name_returned": vendor["legal_name"],
        "name_match_score": 100,
        "urgency_language": True,  # real urgency, not a signal of fraud here
        "amount": vendor["avg_payout_amount"],
        "near_duplicate_invoice": False,
        "split_below_threshold": False,
        "callback_reaches_known_contact": True,  # they pick up, confirm it themselves
    }


SCENARIO_FUNCS = {
    "fraud_easy": scenario_fraud_easy,
    "fraud_hard": scenario_fraud_hard,
    "legit_easy": scenario_legit_easy,
    "legit_hard": scenario_legit_hard,
}

# Fraud oversampled relative to real-world base rates, for statistical power.
# Stated explicitly here and should be stated explicitly in the writeup too.
SCENARIO_WEIGHTS = {
    "fraud_easy": 0.20,
    "fraud_hard": 0.20,
    "legit_easy": 0.35,
    "legit_hard": 0.25,
}


def generate_cases(vendors, n=400):
    types = list(SCENARIO_WEIGHTS.keys())
    weights = list(SCENARIO_WEIGHTS.values())
    cases = []
    for i in range(n):
        vendor = random.choice(vendors)
        scenario_type = random.choices(types, weights=weights, k=1)[0]
        case = SCENARIO_FUNCS[scenario_type](vendor, f"CASE{i:05d}")
        cases.append(case)
    return cases


def split_dev_holdout(cases, holdout_frac=0.30):
    # Stratified by scenario_type so both splits have all four narrative types
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
            print(f"   {t}: {count}")

    print(f"vendor_master.csv: {len(vendors)} vendors\n")
    summarize("cases_dev.csv", dev)
    print()
    summarize("cases_holdout.csv", holdout)


if __name__ == "__main__":
    main()
