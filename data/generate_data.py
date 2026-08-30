"""
PayeeProof — synthetic data generator.

Four outputs:
  vendor_master.csv    — the "trusted source" every request gets checked against
  vendor_accounts.csv  — the accounts on file for each vendor, with provenance
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

ONE ACCOUNT PER VENDOR WAS A LIE, AND IT COST TWO REAL BEHAVIOURS
-----------------------------------------------------------------
Until v2 every vendor had exactly one account. Two consequences, both in
vendor_accounts.csv's reason for existing:

  - A corporate group sharing a bank account was REJECTED, because the account
    index was a dict and the second vendor silently overwrote the first.
  - A vendor with a genuine second account was HELD on every payout to it,
    forever, because the master recorded only one.

So accounts now live in their own table with their own provenance. added_via
records which channel asked for the account, verified_by records what evidence
confirmed it, and settled_payout_count records whether money has ever actually
arrived there. Being listed proves nothing; those three columns are what makes
an account usable as a trust anchor.

THE SECOND VERIFICATION CHANNEL — AND WHY THE OLD COLUMN HAD TO GO
-------------------------------------------------------------------
v1 carried `controls_existing_account`: one bool meaning "a Reverse Penny Drop
from the account on file would succeed". With several accounts on file, "the
account on file" is not a thing — and worse, that column ANSWERED THE QUESTION
THE POLICY IS SUPPOSED TO DECIDE. Which account the system demands proof from is
the entire control; encoding the outcome in the data made it unmeasurable.

Replaced by `requester_controls_accounts`: the accounts this requester can
actually send money out of. A description of the world. The verifier NAMES an
account by policy and the evaluator checks whether that named account is in this
list, so a policy that names badly now shows up as a failure.

It is deliberately NOT a copy of the label, in both directions:
  - legitimate vendors do not always control their old account — a real bank
    switch may mean the old facility is already closed;
  - and one attacker DOES control an account on file. fraud_planted_account is
    the case where they got an account onto the master earlier and now use it to
    satisfy the second channel: a previous success used as the credential for
    the next one.

DATES ARE FROZEN, DELIBERATELY
------------------------------
Account seasoning is a comparison against a date, and the naive version —
`(today - added_on).days` — makes the dataset AGE: a case that holds today
releases in six months, and nothing in the diff explains it. So AS_OF is a fixed
date, every case carries its own request_date, and every account age is measured
against the CASE's date. The corpus reproduces identically in 2030.

Account ages are written as clearly-recent or clearly-seasoned rather than
placed relative to the policy's threshold, so SEASONING_DAYS can be tuned
without regenerating anything.

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
from datetime import date, timedelta

# v2. A new seed, because v1's holdout has been seen and what it showed shaped
# v2's design — the inactive fix and the no-reject decision both came from
# reading it. A new seed does not un-see that; what it buys is that the parts of
# v2 which matter most are tested on ground nothing has been tuned against.
SEED = 20260829
random.seed(SEED)

# The corpus's frozen "now". Nothing here reads the system clock.
AS_OF = date(2026, 6, 30)

# Account ages, written as intent rather than as a threshold. RECENT is what a
# planted account looks like; SEASONED is what a real one does. The policy's
# SEASONING_DAYS sits between them and can move without a regeneration.
RECENT_DAYS   = (5, 45)
SEASONED_DAYS = (200, 1100)

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


def _days_before(anchor: date, span) -> date:
    lo, hi = span
    return anchor - timedelta(days=random.randint(lo, hi))


def _iso(d):
    return d.isoformat() if d else ""


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
    """
    The vendor record itself. Accounts moved to vendor_accounts.csv — keeping
    known_account_number here as well would let the two diverge, and the
    divergence would be silent: the loader reads one, the eval prints the other.

    group_id is DECLARED by the merchant, never inferred from a shared account,
    because a shared account is the thing being judged. Most vendors have none.
    """
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
            "known_domain": "",          # filled below, uniquely
            "known_phone": _phone(),
            "city": city,
            "avg_payout_amount": random.choice([15000, 28000, 45000, 82000, 150000, 310000]),
            "group_id": "",
        })

    # Domains must be UNIQUE. Legal names are drawn from a small pool, so
    # _domain() collided for 57 of 120 vendors — and a domain shared by two
    # unrelated vendors makes the sender an ambiguous identifier, which quietly
    # turns triage's vendor resolution into a coin flip. Disambiguate with the
    # city, then with a counter.
    taken = set()
    for v in vendors:
        base = _domain(v["legal_name"])
        cand = base
        if cand in taken:
            cand = _domain(f"{v['legal_name']} {v['city']}")
        # NOT `n`: that is the vendor-count parameter, and shadowing it here
        # left n == 2 for the group loop below, which then built ONE group
        # instead of twenty. The corpus generated cleanly, every eval ran, and
        # the corporate-group result was quietly computed over three vendors.
        suffix = 2
        while cand in taken:
            cand = base.replace(".com", f"{suffix}.com")
            suffix += 1
        taken.add(cand)
        v["known_domain"] = cand

    # Corporate groups: a minority of the master, declared explicitly. Members
    # are drawn as contiguous blocks only for readability of the CSV; nothing
    # reads the ordering.
    pool = [v for v in vendors]
    random.shuffle(pool)
    cursor = 0
    for g in range(max(1, n // 6)):
        size = random.choice([2, 2, 3])
        members = pool[cursor:cursor + size]
        cursor += size
        if len(members) < 2:
            break
        for v in members:
            v["group_id"] = f"GRP{g:03d}"
    return vendors


def generate_vendor_accounts(vendors):
    """
    The accounts on file, with the provenance that makes them usable — or not —
    as a trust anchor.

      added_via     which channel ASKED for this account. An account added by an
                    email request cannot verify another email request, so this
                    is what breaks the circularity in the second channel.
      verified_by   what evidence CONFIRMED it. An account that entered the
                    master unverified is worthless as an anchor: the destination
                    would be checked against a record an attacker could write.
      settled_*     whether money has ever actually arrived. Being listed is not
                    evidence of anything.

    Distribution: every vendor has one onboarding account; about a quarter have
    a second; a few have a third. Roughly half the declared groups genuinely
    share one account across their members — the case that is rejected today.
    """
    accounts = []
    by_group = {}
    for v in vendors:
        if v["group_id"]:
            by_group.setdefault(v["group_id"], []).append(v)

    # Groups that genuinely share a facility across members.
    shared = {}
    for gid, members in by_group.items():
        if random.random() < 0.6:
            shared[gid] = {"account_number": _account_number(), "ifsc": _ifsc()}

    for v in vendors:
        # About one vendor in twelve is brand new: onboarded recently, never
        # paid. Nothing exists to compare a request against, which is a real
        # state and the one fraud_first_contact exploits.
        brand_new = random.random() < 0.08
        opened = (_days_before(AS_OF, (10, 70)) if brand_new
                  else _days_before(AS_OF, (400, 1600)))
        accounts.append({
            "vendor_id": v["vendor_id"],
            "account_number": _account_number(),
            "ifsc": _ifsc(),
            "status": "active",
            "added_on": _iso(opened),
            "added_via": "onboarding",
            "verified_by": "onboarding_kyc",
            "verified_on": _iso(opened),
            "settled_payout_count": 0 if brand_new else random.randint(6, 90),
            "last_settled_on": "" if brand_new else _iso(_days_before(AS_OF, (3, 45))),
            "is_primary": True,
        })
        if brand_new:
            continue          # no second facility before the first payment

        # A second, genuinely theirs: a division, a collections facility, a
        # bank the treasury moved part of the business to.
        if random.random() < 0.26:
            added = _days_before(AS_OF, SEASONED_DAYS)
            settled = random.randint(1, 20)
            accounts.append({
                "vendor_id": v["vendor_id"],
                "account_number": _account_number(),
                "ifsc": _ifsc(),
                "status": "active",
                "added_on": _iso(added),
                "added_via": random.choice(["portal", "email_request", "phone_request"]),
                "verified_by": random.choice(["penny_drop", "callback"]),
                "verified_on": _iso(added + timedelta(days=random.randint(0, 4))),
                "settled_payout_count": settled,
                "last_settled_on": _iso(_days_before(AS_OF, (10, 200))),
                "is_primary": False,
            })

            if random.random() < 0.30:
                added3 = _days_before(AS_OF, SEASONED_DAYS)
                accounts.append({
                    "vendor_id": v["vendor_id"],
                    "account_number": _account_number(),
                    "ifsc": _ifsc(),
                    "status": random.choice(["active", "active", "dormant"]),
                    "added_on": _iso(added3),
                    "added_via": "portal",
                    "verified_by": "penny_drop",
                    "verified_on": _iso(added3),
                    "settled_payout_count": random.randint(0, 6),
                    "last_settled_on": _iso(_days_before(AS_OF, (60, 400))),
                    "is_primary": False,
                })

        # The shared group facility, listed under every member. Legitimate, and
        # indistinguishable from the mule pattern without group_id — which is
        # precisely why the code rejects it today.
        gid = v["group_id"]
        if gid in shared:
            added = _days_before(AS_OF, SEASONED_DAYS)
            accounts.append({
                "vendor_id": v["vendor_id"],
                "account_number": shared[gid]["account_number"],
                "ifsc": shared[gid]["ifsc"],
                "status": "active",
                "added_on": _iso(added),
                "added_via": "portal",
                "verified_by": "onboarding_kyc",
                "verified_on": _iso(added),
                "settled_payout_count": random.randint(2, 30),
                "last_settled_on": _iso(_days_before(AS_OF, (5, 90))),
                "is_primary": False,
            })

    return accounts, shared


# ---------------------------------------------------------------------------
# Scenario narratives — label comes from the story, features follow from it
# ---------------------------------------------------------------------------

def _accounts_of(vendor, ctx):
    return ctx["by_vendor"].get(vendor["vendor_id"], [])


def _primary_of(vendor, ctx):
    for a in _accounts_of(vendor, ctx):
        if a["is_primary"]:
            return a
    return _accounts_of(vendor, ctx)[0]


def _controls_all(vendor, ctx):
    """
    Every account this vendor can send money out of — the legitimate case.

    Planted accounts are excluded. An account an attacker got onto the master
    is on file for this vendor and is NOT controlled by them; including it here
    would hand the legitimate requester a credential belonging to the attacker
    and quietly destroy the scenario it was planted for.
    """
    return ";".join(a["account_number"] for a in _accounts_of(vendor, ctx)
                    if a["status"] == "active"
                    and a["account_number"] not in ctx["planted"])


def _add_account(ctx, vendor, case_id, **overrides):
    """
    An account that entered the master because of an earlier request, rather
    than at onboarding. The point of modelling this at all: v1 modelled the
    REQUEST to add an account and never the RESULTING STATE, so ADD versus
    REPLACE could not be tested end to end — the distinction R4's design rests
    on — and the planted-account attack had nowhere to live.
    """
    row = {
        "vendor_id": vendor["vendor_id"],
        "account_number": _account_number(),
        "ifsc": _ifsc(),
        "status": "active",
        "added_on": "",
        "added_via": "email_request",
        "verified_by": "unverified",
        "verified_on": "",
        "settled_payout_count": 0,
        "last_settled_on": "",
        "is_primary": False,
    }
    row.update(overrides)
    ctx["accounts"].append(row)
    ctx["by_vendor"].setdefault(vendor["vendor_id"], []).append(row)
    return row


def _base(vendor, case_id, scenario_type, label, ctx):
    """Fields every scenario shares; each story then overrides what it changes."""
    status, name_ok = _fav()
    primary = _primary_of(vendor, ctx)
    return {
        "case_id": case_id, "vendor_id": vendor["vendor_id"], "label": label,
        "scenario_type": scenario_type,
        # The case's own "now". Every age in this corpus is measured against
        # this date and never against the system clock, so the dataset does not
        # change its answers as it gets older.
        "request_date": _iso(_days_before(AS_OF, (0, 150))),
        "action_type": "REPLACE",
        "sender_domain": vendor["known_domain"],
        "sender_phone_used": vendor["known_phone"],
        "proposed_gstin": vendor["gstin"],
        "proposed_account_number": primary["account_number"],
        "proposed_ifsc": primary["ifsc"],
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
        # Which accounts can the requester actually move money OUT of? A Reverse
        # Penny Drop asks exactly that, of ONE named account. This is a
        # description of the world; which account gets named is the policy's
        # job, and keeping the two apart is what makes the policy measurable.
        "requester_controls_accounts": _controls_all(vendor, ctx),
        # For V2.3. Nothing reads these yet.
        "thread_id": f"THR{case_id[-5:]}",
        "is_reply": False,
    }


# ── Fraud ─────────────────────────────────────────────────────────────

def scenario_fraud_easy(vendor, case_id, ctx):
    """Attacker compromises vendor's email, uses a lookalike domain, sends an
    invoice with a materially altered GSTIN and a brand-new account under a name
    that doesn't match the vendor master at all. The loud, unsubtle version."""
    c = _base(vendor, case_id, "fraud_easy", "fraud", ctx)
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
        "requester_controls_accounts": "",
    })
    return c


def scenario_fraud_hard(vendor, case_id, ctx):
    """Attacker did their homework — correct GSTIN, domain off by one character,
    real vendor name registered on an account they control. Amount split just
    under a plausible approval threshold. Only the destination and a faint
    urgency cue give it away."""
    c = _base(vendor, case_id, "fraud_hard", "fraud", ctx)
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
        "requester_controls_accounts": "",
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
    c = _base(vendor, case_id, "fraud_compromised", "fraud", ctx)
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(88, 100),
        # Patient attacker: often no urgency tell at all.
        "urgency_language": random.random() < 0.35,
        "channel_manipulation": random.random() < 0.40,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.08),
        "callback_reaches_known_contact": False,
        "requester_controls_accounts": "",
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
    c = _base(vendor, case_id, "fraud_mule", "fraud", ctx)
    # Not a group sibling: an account shared inside a declared group is
    # legitimate, and drawing the mule from one would mislabel it as fraud.
    others = [v for v in ctx["vendors"]
              if v["vendor_id"] != vendor["vendor_id"]
              and not (vendor["group_id"] and v["group_id"] == vendor["group_id"])]
    mule = _primary_of(random.choice(others), ctx)["account_number"]
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
        "requester_controls_accounts": "",
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
    c = _base(vendor, case_id, "fraud_sim_swap", "fraud", ctx)
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
        "requester_controls_accounts": "",
    })
    return c


# ── Legitimate ────────────────────────────────────────────────────────

def scenario_legit_easy(vendor, case_id, ctx):
    """Routine payout to a long-standing, unchanged account. Nothing should trip."""
    c = _base(vendor, case_id, "legit_easy", "legit", ctx)
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
    c = _base(vendor, case_id, "legit_hard", "legit", ctx)
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        # A genuine switch sometimes means the old facility is already closed.
        "requester_controls_accounts": (_controls_all(vendor, ctx)
                                        if _stable_bool(case_id, "rpd", 0.75) else ""),
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
    c = _base(vendor, case_id, "legit_rebrand", "legit", ctx)
    c.update({
        "sender_domain": _rebranded_domain(vendor["known_domain"]),
        # An acquisition often consolidates banking, so the old facility may
        # already be gone.
        "requester_controls_accounts": (_controls_all(vendor, ctx)
                                        if _stable_bool(case_id, "rpd", 0.65) else ""),
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
    c = _base(vendor, case_id, "legit_add_account", "legit", ctx)
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
    c = _base(vendor, case_id, "legit_unreachable", "legit", ctx)
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "urgency_language": random.random() < 0.40,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.10),
        "callback_reaches_known_contact": False,
        # Nobody answers the phone, but the vendor still banks where they always
        # have. This is the scenario the second channel should RESCUE: a genuine
        # request currently held for want of a phone call. Inherited from _base.
    })
    return c


# ── New in v2 ─────────────────────────────────────────────────────────

def scenario_legit_group_shared_account(vendor, case_id, ctx):
    """
    Two companies in one declared corporate group settle into a single treasury
    facility. Routine payout, nothing is being changed, and the destination is
    an account that is also on file under a sibling vendor.

    THIS IS REJECTED BY v1, in three lines: build_account_index() assigns
    account -> vendor in a loop, so the sibling silently overwrites, and the
    payout then fires R2c_followup_destination_conflict. decision_engine's own
    comment says sharing an account across contacts is legitimate for corporate
    groups; the code rejected it anyway. The scenario exists to hold that fixed.
    """
    c = _base(vendor, case_id, "legit_group_shared_account", "legit", ctx)
    shared = ctx["shared"][vendor["group_id"]]
    c.update({
        "action_type": "NONE",
        "proposed_account_number": shared["account_number"],
        "proposed_ifsc": shared["ifsc"],
        "amount": _amount_near(vendor["avg_payout_amount"], 0.08),
    })
    return c


def scenario_legit_second_account(vendor, case_id, ctx):
    """
    A vendor with more than one facility of their own — a division, a
    collections account — being paid into the one that is not primary. Nothing
    is being requested; this is just where this invoice settles.

    Under v1 this produced account_continuity WARN on EVERY payout to that
    account, forever, because the master recorded one account per vendor. A
    permanent false-hold generator, and invisible while the data agreed with
    the bug.
    """
    c = _base(vendor, case_id, "legit_second_account", "legit", ctx)
    others = [a for a in _accounts_of(vendor, ctx)
              if not a["is_primary"] and a["status"] == "active"
              and a["verified_by"] != "unverified"
              and a["account_number"] not in ctx["planted"]]
    acct = random.choice(others)
    c.update({
        "action_type": "NONE",
        "proposed_account_number": acct["account_number"],
        "proposed_ifsc": acct["ifsc"],
        "amount": _amount_near(vendor["avg_payout_amount"], 0.10),
    })
    return c


def scenario_legit_added_then_paid(vendor, case_id, ctx):
    """
    The state v1 could not represent. The vendor asked to add an account some
    time ago, the request was verified and accepted, the account went onto the
    master — and now an invoice settles into it.

    v1 modelled the REQUEST (legit_add_account) and never the RESULTING STATE,
    so a payout to an added account stayed "new" indefinitely and ADD versus
    REPLACE could not be tested end to end. That distinction is what R4's design
    rests on.
    """
    c = _base(vendor, case_id, "legit_added_then_paid", "legit", ctx)
    added = _days_before(AS_OF, SEASONED_DAYS)
    acct = _add_account(ctx, vendor, case_id,
                        added_on=_iso(added),
                        added_via="email_request",
                        verified_by="callback",
                        verified_on=_iso(added + timedelta(days=1)),
                        settled_payout_count=random.randint(1, 12),
                        last_settled_on=_iso(_days_before(AS_OF, (20, 180))))
    c.update({
        "action_type": "NONE",
        "proposed_account_number": acct["account_number"],
        "proposed_ifsc": acct["ifsc"],
        "amount": _amount_near(vendor["avg_payout_amount"], 0.12),
    })
    return c


def scenario_fraud_planted_account(vendor, case_id, ctx):
    """
    THE ATTACK THE SECOND CHANNEL IS BLIND TO, and the only case in this corpus
    where an attacker controls an account already on file.

    Earlier, the attacker got account B added to the vendor master — a
    compromised mailbox and a plausible "we have opened a second facility"
    request, which is exactly the legit_add_account narrative. It went on
    unverified. Now they ask for the destination to move to account C.

    Ask "prove you control an account on file" and the attacker penny-drops from
    B. The strongest control in the system confirms the fraud, because they used
    a PREVIOUS SUCCESS as the credential for the next one.

    B is written to look like what it is: added by an email request, never
    verified, recent, and never paid. Any policy that names the account to drop
    from — rather than letting the requester choose — refuses it on all four.
    """
    c = _base(vendor, case_id, "fraud_planted_account", "fraud", ctx)
    planted = _add_account(ctx, vendor, case_id,
                           added_on=_iso(_days_before(AS_OF, RECENT_DAYS)),
                           added_via="email_request",
                           verified_by="unverified",
                           settled_payout_count=0)
    ctx["planted"].add(planted["account_number"])
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(82, 100),
        "urgency_language": random.random() < 0.45,
        "channel_manipulation": random.random() < 0.40,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.10),
        "callback_reaches_known_contact": False,
        # The whole point: they DO control an account on file.
        "requester_controls_accounts": planted["account_number"],
    })
    return c


def scenario_fraud_first_contact(vendor, case_id, ctx):
    """
    A supplier the buyer has onboarded but never actually paid, impersonated on
    what would be the first invoice. There is no payment history to compare
    against and no seasoned account to demand proof from.

    Holding is the correct outcome and the honest one: nothing exists to check.
    The scenario is here so that "we could not check" is measured rather than
    assumed, and so the second channel's unavailable state has real cases.
    """
    c = _base(vendor, case_id, "fraud_first_contact", "fraud", ctx)
    c.update({
        "sender_domain": _lookalike_domain(vendor["known_domain"]),
        "sender_phone_used": _phone(),
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(60, 95),
        "urgency_language": random.random() < 0.55,
        "channel_manipulation": random.random() < 0.35,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.25),
        "callback_reaches_known_contact": False,
        "requester_controls_accounts": "",
    })
    return c


def scenario_fraud_thread_hijack(vendor, case_id, ctx):
    """
    The attacker is inside a conversation that is already real: same thread,
    same participants, correct invoice numbers, replying from the vendor's own
    mailbox. Nothing about the channel is wrong and there is no new sender to
    notice.

    Here for V2.3 — thread depth and first-contact history are inbox signals,
    and nothing in v2's rule table reads them yet. Recorded rather than claimed.
    """
    c = _base(vendor, case_id, "fraud_thread_hijack", "fraud", ctx)
    c.update({
        "proposed_account_number": _account_number(),
        "proposed_ifsc": _ifsc(),
        "name_match_score": random.randint(85, 100),
        "urgency_language": random.random() < 0.25,
        "channel_manipulation": random.random() < 0.20,
        "amount": _amount_near(vendor["avg_payout_amount"], 0.07),
        "callback_reaches_known_contact": False,
        "requester_controls_accounts": "",
        "is_reply": True,
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
    "fraud_planted_account": scenario_fraud_planted_account,
    "fraud_first_contact": scenario_fraud_first_contact,
    "fraud_thread_hijack": scenario_fraud_thread_hijack,
    "legit_group_shared_account": scenario_legit_group_shared_account,
    "legit_second_account": scenario_legit_second_account,
    "legit_added_then_paid": scenario_legit_added_then_paid,
}

# Scenarios that need the vendor to be in a particular state. Drawing the
# vendor at random and hoping would silently produce a handful of cases and a
# misleading per-scenario n.
VENDOR_POOL = {
    "legit_group_shared_account": "shared_group",
    "legit_second_account": "multi_account",
    "fraud_first_contact": "new_vendor",
}

# Fraud is oversampled relative to real-world base rates, for statistical power.
# Stated explicitly here and stated explicitly in the writeup too. Nothing in
# these weights is derived from what the rules currently catch — the hard and
# adversarial scenarios are weighted UP precisely because they are where the
# rules are expected to struggle.
SCENARIO_WEIGHTS = {
    "fraud_easy":                 0.07,
    "fraud_hard":                 0.10,
    "fraud_compromised":          0.09,
    "fraud_mule":                 0.05,
    "fraud_sim_swap":             0.05,
    "fraud_planted_account":      0.05,
    "fraud_first_contact":        0.04,
    "fraud_thread_hijack":        0.04,
    "legit_easy":                 0.12,
    "legit_hard":                 0.11,
    "legit_rebrand":              0.08,
    "legit_add_account":          0.05,
    "legit_unreachable":          0.04,
    "legit_group_shared_account": 0.04,
    "legit_second_account":       0.04,
    "legit_added_then_paid":      0.03,
}


def _build_context(vendors, accounts, shared):
    by_vendor = {}
    for a in accounts:
        by_vendor.setdefault(a["vendor_id"], []).append(a)

    by_id = {v["vendor_id"]: v for v in vendors}

    def has_extra(v):
        return sum(1 for a in by_vendor.get(v["vendor_id"], [])
                   if not a["is_primary"] and a["status"] == "active") >= 1

    def is_new(v):
        return all(int(a["settled_payout_count"]) == 0
                   for a in by_vendor.get(v["vendor_id"], []))

    return {
        "vendors": vendors,
        "by_id": by_id,
        "accounts": accounts,          # appended to by the scenarios themselves
        "by_vendor": by_vendor,
        "shared": shared,
        "planted": set(),
        "pools": {
            "shared_group": [v for v in vendors if v["group_id"] in shared],
            "multi_account": [v for v in vendors if has_extra(v)],
            "new_vendor": [v for v in vendors if is_new(v)],
        },
    }


def generate_cases(vendors, accounts, shared, n=400):
    types = list(SCENARIO_WEIGHTS.keys())
    weights = list(SCENARIO_WEIGHTS.values())
    ctx = _build_context(vendors, accounts, shared)
    cases = []
    for i in range(n):
        # Scenario first, THEN a vendor that can actually carry it. Drawing the
        # vendor first and rejecting it afterwards would bias the vendor mix.
        scenario_type = random.choices(types, weights=weights, k=1)[0]
        pool = ctx["pools"].get(VENDOR_POOL.get(scenario_type, ""), vendors)
        if not pool:
            raise RuntimeError(
                f"no vendor can carry {scenario_type}; the master generated "
                f"none in that state, and silently substituting a different "
                f"vendor would mislabel the case")
        vendor = random.choice(pool)
        cases.append(SCENARIO_FUNCS[scenario_type](vendor, f"CASE{i:05d}", ctx))
    return cases, ctx


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
        # The explicit LF terminator is NOT cosmetic. csv.writer defaults to
        # CRLF, git stores these blobs as LF, and Windows autocrlf hides the
        # mismatch by normalising both sides of every comparison. On Linux
        # nothing normalises, so a regenerated file differed from the committed
        # one byte for byte and CI's "committed data still matches the
        # generator" step failed on every push — while passing locally the
        # whole time, which is the worst shape a check can have.
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    # Write beside this file, not into the caller's cwd — pipeline.load_vendors()
    # looks for repo_root/data/vendor_master.csv.
    here = os.path.dirname(os.path.abspath(__file__))

    vendors = generate_vendor_master(n=120)
    accounts, shared = generate_vendor_accounts(vendors)

    # Cases run BEFORE the accounts file is written: scenarios that model an
    # account being added put it on the master, which is the whole point of
    # legit_added_then_paid and fraud_planted_account.
    cases, ctx = generate_cases(vendors, accounts, shared, n=800)

    write_csv(os.path.join(here, "vendor_master.csv"), vendors)
    write_csv(os.path.join(here, "vendor_accounts.csv"), ctx["accounts"])

    dev, holdout = split_dev_holdout(cases, holdout_frac=0.30)
    write_csv(os.path.join(here, "cases_dev.csv"), dev)
    write_csv(os.path.join(here, "cases_holdout.csv"), holdout)

    def summarize(name, rows):
        fraud = sum(1 for r in rows if r["label"] == "fraud")
        print(f"{name}: {len(rows)} cases, {fraud} fraud / {len(rows) - fraud} legit")
        for t in SCENARIO_WEIGHTS:
            count = sum(1 for r in rows if r["scenario_type"] == t)
            print(f"   {t:20s} {count}")

    grouped = sum(1 for v in vendors if v["group_id"])
    per_vendor = {}
    for a in ctx["accounts"]:
        per_vendor[a["vendor_id"]] = per_vendor.get(a["vendor_id"], 0) + 1
    spread = {k: sum(1 for c in per_vendor.values() if c == k)
              for k in sorted(set(per_vendor.values()))}
    print(f"vendor_master.csv:   {len(vendors)} vendors, {grouped} in a declared group")
    print(f"vendor_accounts.csv: {len(ctx['accounts'])} accounts, "
          f"vendors by account count {spread}")
    print(f"seed {SEED}, as-of {AS_OF}\n")
    summarize("cases_dev.csv", dev)
    print()
    summarize("cases_holdout.csv", holdout)


if __name__ == "__main__":
    main()
