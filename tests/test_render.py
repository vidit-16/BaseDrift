"""
BaseDrift — renderer tests.

Two properties carry the whole extraction evaluation:

  NO LEAKAGE   If the rendered messages contain the keyword baseline's trigger
               vocabulary, the extraction eval measures word overlap rather than
               understanding. The README records the project making exactly that
               mistake once, in ablation corpus v1.

  DETERMINISM  A rendered message must be reproducible from its case id alone,
               independently of how many other cases were rendered first, or an
               extraction result cannot be tied to the text that produced it.

No API key, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"))

import render as R  # noqa: E402
from ablation import keyword_baseline  # noqa: E402

VENDOR = {"vendor_id": "VEND0069", "legal_name": "Balaji Logistics",
          "gstin": "07JQQPG8009O1Z2", "known_domain": "balajilogistic.com"}


def row(**over):
    r = {
        "case_id": "CASE00001", "vendor_id": "VEND0069",
        "action_type": "REPLACE", "sender_domain": "balaj1logistic.com",
        "proposed_account_number": "351349409853", "proposed_ifsc": "KKBK0238196",
        "proposed_gstin": "07JQQPG8009O1Z2", "amount": "28000",
        "urgency_language": "False", "channel_manipulation": "False",
        "hedged_gstin": "False",
    }
    r.update(over)
    return r


# ── No leakage ───────────────────────────────────────────────────────

def test_every_template_is_free_of_baseline_vocabulary():
    """
    Checked at the source, so a leaking template fails here rather than
    surfacing as a suspiciously strong baseline score much later.
    """
    pools = {n: v for n, v in vars(R).items()
             if n.isupper() and isinstance(v, list) and v
             and isinstance(v[0], str)
             and n not in ("BANNED_VOCABULARY", "BANNED_LABELS")}
    assert pools, "no template pools found"
    for name, pool in pools.items():
        for tpl in pool:
            R.assert_no_leakage(tpl, name)


def test_guard_actually_catches_a_trigger_word():
    """A guard that cannot fail is decoration."""
    for bad in ("Please update our bank details.",
                "We want to add another account.",
                "Use this instead of the old one.",
                "Settle the outstanding invoices here."):
        try:
            R.assert_no_leakage(bad, "T")
            raise AssertionError(f"guard missed: {bad!r}")
        except R.LeakageError:
            pass


def test_guard_catches_ground_truth_vocabulary():
    for bad in ("this is a fraud case", "legit vendor", "the attacker's account"):
        try:
            R.assert_no_leakage(bad, "T")
            raise AssertionError(f"guard missed: {bad!r}")
        except R.LeakageError:
            pass


def test_banned_vocabulary_comes_from_the_baseline_itself():
    """Retyping the list would let the two drift apart silently."""
    import ablation
    for w in ablation.CHANGE_TRIGGERS + ablation.ADD_TRIGGERS:
        assert w.lower() in R.BANNED_VOCABULARY, w


def test_rendered_corpus_defeats_the_keyword_baseline():
    """
    The end-to-end check. A HIGH score here would mean the renderer recreated
    the v1 leakage and the extraction numbers would be void.
    """
    cases = R.render_split("dev")
    exact = sum(
        keyword_baseline(c.email) == (c.expected["intent"], c.expected["action"],
                                      c.expected["scope"])
        for c in cases)
    assert exact / len(cases) < 0.30, (
        f"baseline scored {exact}/{len(cases)} — the renderer leaked vocabulary")


# ── Determinism ──────────────────────────────────────────────────────

def test_same_case_renders_identically():
    a = R.render_case(row(), VENDOR)
    b = R.render_case(row(), VENDOR)
    assert a.email == b.email
    assert a.sha256 == b.sha256


def test_rendering_is_independent_of_order():
    """
    Global random state would make a case depend on how many were rendered
    before it, so it could not be reproduced in isolation.
    """
    alone = R.render_case(row(case_id="CASE00042"), VENDOR).sha256
    for i in range(20):
        R.render_case(row(case_id=f"CASE{i:05d}"), VENDOR)
    after = R.render_case(row(case_id="CASE00042"), VENDOR).sha256
    assert alone == after


def test_different_cases_render_differently():
    seen = {R.render_case(row(case_id=f"CASE{i:05d}"), VENDOR).sha256
            for i in range(30)}
    assert len(seen) > 20, "templates are not varying enough"


def test_hash_matches_the_text():
    import hashlib
    c = R.render_case(row(), VENDOR)
    assert c.sha256 == hashlib.sha256(c.email.encode()).hexdigest()


def test_version_is_recorded():
    assert R.render_case(row(), VENDOR).renderer_version == R.RENDERER_VERSION


# ── The message must actually carry the features ─────────────────────

def test_account_and_ifsc_appear_in_the_message():
    c = R.render_case(row(), VENDOR)
    assert "351349409853" in c.email
    assert "KKBK0238196" in c.email


def test_sender_domain_is_in_the_from_header():
    c = R.render_case(row(sender_domain="balaj1logistic.com"), VENDOR)
    assert "@balaj1logistic.com" in c.email.splitlines()[0]


def test_pressure_signals_appear_only_when_flagged():
    """
    Asserts the templates themselves, not the length.

    It used to compare len(urgent) > len(plain), which held only while an
    unflagged email had nothing appended. Controls broke that — an unflagged
    message can now carry "no rush at all, whenever the next run happens is
    fine", which is longer than some urgency lines and is precisely the point
    of it existing. The proxy was measuring the wrong thing all along; it just
    had not been wrong yet.
    """
    def has(email, pool):
        return any(t.format(inv=0, gstin="", amount=0) in email
                   if "{" in t else t in email for t in pool)

    plain = R.render_case(row(case_id="CASE00007"), VENDOR).email
    urgent = R.render_case(row(case_id="CASE00007",
                               urgency_language="True"), VENDOR).email
    assert has(urgent, R.TIME_PRESSURE), "no urgency line when flagged"
    assert not has(plain, R.TIME_PRESSURE), "urgency line without the flag"

    chan = R.render_case(row(case_id="CASE00007",
                             channel_manipulation="True"), VENDOR).email
    assert has(chan, R.CHANNEL_REDIRECT), "no redirect line when flagged"
    assert not has(plain, R.CHANNEL_REDIRECT), "redirect line without the flag"

    # And a control is not a flagged signal, however it reads.
    assert not has(plain, R.TIME_PRESSURE + R.CHANNEL_REDIRECT)


def test_hedged_gstin_reads_as_hedged():
    plain = R.render_case(row(case_id="CASE00009"), VENDOR).email
    hedged = R.render_case(row(case_id="CASE00009", hedged_gstin="True"), VENDOR).email
    assert plain != hedged
    assert any(w in hedged.lower() for w in ("should be", "as far as", "confirm"))


# ── Expectations must match the narrative ────────────────────────────

def test_semantics_follow_the_action_type():
    assert R.render_case(row(action_type="REPLACE"), VENDOR).expected["action"] \
        == "REPLACE_PAYOUT_DESTINATION"
    assert R.render_case(row(action_type="ADD"), VENDOR).expected["action"] \
        == "ADD_FUND_ACCOUNT"
    assert R.render_case(row(action_type="NONE"), VENDOR).expected["intent"] \
        == "PAYMENT_FOLLOWUP"


def test_a_followup_proposes_no_account():
    """
    The field is proposed_account_number. A message restating where payment has
    always gone proposes nothing, so None is the correct reading — expecting the
    number here scored the extractor wrong for being right.
    """
    c = R.render_case(row(action_type="NONE"), VENDOR)
    assert c.expected["account_number"] is None
    assert c.expected["ifsc"] is None
    # The number is still present in the text; it just is not a proposal.
    assert "351349409853" in c.email


def test_a_change_does_propose_an_account():
    c = R.render_case(row(action_type="REPLACE"), VENDOR)
    assert c.expected["account_number"] == "351349409853"


# ── Runner ───────────────────────────────────────────────────────────

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
