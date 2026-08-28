"""
PayeeProof — evaluation harness tests.

The harness had a bug that would have silently corrupted every future
measurement: it cached whatever came back from the model client, including
transport failures. A rate-limited run wrote 201 "extraction failed" records
that would never be retried, and any later scoring pass would have read them
back as a 56% extraction failure rate that was really the network.

That is the same category error the decision engine made with WARN — "I could
not reach the API" says nothing about whether the model can read an email, and
recording it as though it did is worse than not measuring at all, because it
looks like data.

No API key, no network: the model client is stubbed.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "data"))
sys.path.insert(0, os.path.join(HERE, "..", "eval"))

import extraction_eval as X  # noqa: E402
import extractor as E  # noqa: E402
import render as R  # noqa: E402


VENDOR = {"vendor_id": "VEND0069", "legal_name": "Balaji Logistics",
          "gstin": "07JQQPG8009O1Z2", "known_domain": "balajilogistic.com"}


def a_case(case_id="CASE00001"):
    return R.render_case({
        "case_id": case_id, "vendor_id": "VEND0069", "action_type": "REPLACE",
        "sender_domain": "balaj1logistic.com",
        "proposed_account_number": "351349409853", "proposed_ifsc": "KKBK0238196",
        "proposed_gstin": "07JQQPG8009O1Z2", "amount": "28000",
        "urgency_language": "False", "channel_manipulation": "False",
        "hedged_gstin": "False",
    }, VENDOR)


class temp_cache:
    def __enter__(self):
        self._real = X.CACHE_DIR
        X.CACHE_DIR = tempfile.mkdtemp(prefix="pp_cache_")
        return X.CACHE_DIR

    def __exit__(self, *a):
        shutil.rmtree(X.CACHE_DIR, ignore_errors=True)
        X.CACHE_DIR = self._real
        return False


class stub_extract:
    """Replace extractor.extract with a fixed result."""

    def __init__(self, result, count=None):
        self.result, self.count = result, count if count is not None else []

    def __enter__(self):
        self._real = E.extract

        def fake(text, model=None):
            self.count.append(1)
            return self.result

        E.extract = fake
        return self

    def __exit__(self, *a):
        E.extract = self._real
        return False


# ── Classifying a failure ────────────────────────────────────────────

def test_transport_failures_are_recognised():
    for reason in [
        "HTTP 429: Rate limit reached for model `openai/gpt-oss-120b`",
        "network error: HTTPSConnectionPool(host='api.groq.com', port=443)",
        "request timed out",
        "exhausted retries on rate limit",
        "network error: ('Connection aborted.', RemoteDisconnected())",
    ]:
        assert X.is_transient(reason), reason


def test_real_extraction_failures_are_not_transport():
    """These are findings about the model, and must be kept."""
    for reason in [
        "schema validation failed: invalid intent: 'NONSENSE'",
        "malformed provider output (AttributeError: ...)",
        "document is empty after sanitization",
        "JSON parse failed (Expecting value): not json",
        None,
    ]:
        assert not X.is_transient(reason), reason


# ── The cache must never persist a network problem ───────────────────

def test_transport_failure_is_never_cached():
    """
    The bug. A rate-limited call previously wrote an 'extraction failed' record
    that no later run would retry.
    """
    failure = E.ExtractionResult(ok=False, failure_reason="HTTP 429: Rate limit")
    with temp_cache() as cache_dir, stub_extract(failure):
        X.TRANSIENT_BACKOFF = (0, 0, 0)      # do not actually sleep in a test
        try:
            X.extract_cached(a_case(), "m", 1)
            raise AssertionError("should have raised RateLimited")
        except X.RateLimited:
            pass
        assert os.listdir(cache_dir) == [], "a transport failure was written"


def test_transport_failure_is_retried_before_giving_up():
    calls = []
    failure = E.ExtractionResult(ok=False, failure_reason="network error: boom")
    with temp_cache(), stub_extract(failure, calls):
        X.TRANSIENT_BACKOFF = (0, 0, 0)
        try:
            X.extract_cached(a_case(), "m", 1)
        except X.RateLimited:
            pass
    assert len(calls) == 1 + len(X.TRANSIENT_BACKOFF)


def test_a_genuine_extraction_failure_IS_cached():
    """
    It is a real result about the model, so re-running must not pay for it
    again — and must not quietly turn it into a success.
    """
    failure = E.ExtractionResult(
        ok=False, failure_reason="schema validation failed: invalid intent")
    with temp_cache() as cache_dir, stub_extract(failure):
        d, from_cache = X.extract_cached(a_case(), "m", 1)
        assert from_cache is False
        assert d["ok"] is False
        assert len(os.listdir(cache_dir)) == 1

        d2, from_cache2 = X.extract_cached(a_case(), "m", 1)
        assert from_cache2 is True
        assert d2["ok"] is False


def test_success_is_cached_and_reused():
    good = E.ExtractionResult(
        ok=True, intent=E.INTENT_CHANGE, action=E.ACTION_REPLACE,
        scope=E.SCOPE_BOTH, proposed_account_number="351349409853")
    calls = []
    with temp_cache(), stub_extract(good, calls):
        X.extract_cached(a_case(), "m", 1)
        X.extract_cached(a_case(), "m", 1)
    assert len(calls) == 1, "second call should have come from cache"


def test_cached_payload_records_its_provenance():
    good = E.ExtractionResult(ok=True, intent=E.INTENT_FOLLOWUP,
                              action=E.ACTION_NONE, scope=E.SCOPE_NONE)
    with temp_cache() as cache_dir, stub_extract(good):
        c = a_case()
        X.extract_cached(c, "some-model", 1)
        blob = json.load(open(os.path.join(cache_dir, os.listdir(cache_dir)[0]),
                              encoding="utf-8"))
    meta = blob["_meta"]
    assert meta["model"] == "some-model"
    assert meta["prompt_hash"] == X.PROMPT_HASH
    assert meta["email_sha256"] == c.sha256
    assert meta["renderer_version"] == R.RENDERER_VERSION


# ── Cache keys must invalidate when they should ──────────────────────

def test_key_separates_model_prompt_normaliser_and_run():
    c = a_case()
    base = X.cache_path(c.sha256, "m1", 1)
    assert base != X.cache_path(c.sha256, "m2", 1)
    assert base != X.cache_path(c.sha256, "m1", 2)
    assert X.NORMALIZER_VERSION in os.path.basename(base)
    assert X.PROMPT_HASH in os.path.basename(base)


def test_different_emails_get_different_keys():
    assert X.cache_path(a_case("CASE00001").sha256, "m", 1) != \
        X.cache_path(a_case("CASE00002").sha256, "m", 1)


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
