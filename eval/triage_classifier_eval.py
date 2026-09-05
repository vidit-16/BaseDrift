"""
BaseDrift — triage stage 4, measured.

Stage 4 is the only part of the funnel that calls a model, and until now it was
the only part with no number attached. The eval printed the deterministic
pre-read's behaviour as a FLOOR and said so, which was honest but is not a
measurement.

    python eval/triage_classifier_eval.py                # 400-message sample
    python eval/triage_classifier_eval.py --limit 0      # everything (costly)
    python eval/triage_classifier_eval.py --split holdout

WHAT IS BEING ASKED
===================
"Should the decision engine see this message?" — not "is this fraud", which
stage 4 has no evidence for and never decides.

That question includes PAYMENT FOLLOW-UPS, not only change requests. A follow-up
claims nothing is changing, and R2 exists to check that claim against the real
destination: R2c catches the mule pattern on exactly such a message. Dropping
follow-ups at triage would remove a control, so ground truth counts all 552
corpus cases as true and all inbox noise as false.

THAT MAKES THIS HARD ON PURPOSE
================================
115 of the corpus cases are follow-ups that restate the account and IFSC while
chasing an invoice. The noise contains 1,168 chasers and 1,719 invoices, and the
invoices reprint standing bank details because real ones do. The two populations
overlap in vocabulary, in structure, and in the presence of an account number.
A corpus where they did not overlap would flatter the classifier.

THE BASELINE IT HAS TO BEAT
===========================
looks_like_it_touches_money() — the deterministic pre-read that runs when no
classifier is supplied. Every number below is reported against it, because a
model that does not beat the free thing it replaces is not worth a call.

COST CONTROL
============
Verdicts are cached by message hash, model and prompt hash, so re-scoring is
free and an interrupted run resumes. Transport failures are never cached: a
rate-limited run that persisted "not a change request" would silently poison
every later pass, which has happened once already in this project.
"""

import argparse
import collections
import csv
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import llm_client  # noqa: E402
import pipeline  # noqa: E402
import triage as T  # noqa: E402

DATA = os.path.join(HERE, "..", "data")
CACHE_DIR = os.path.join(HERE, ".classifier_cache")
TRANSIENT_BACKOFF = (20, 60, 150)


class RateLimited(RuntimeError):
    pass


def is_transient(reason):
    r = (reason or "").lower()
    return any(x in r for x in ("429", "rate limit", "timed out", "timeout",
                                "502", "503", "504", "network error"))


def cache_path(msg, model):
    key = hashlib.sha256(
        f"{msg.sha256()}|{model}|{T.CLASSIFIER_PROMPT_HASH}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.json")


def classify_cached(msg, classifier, model):
    path = cache_path(msg, model)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True

    last = None
    for wait in (0,) + TRANSIENT_BACKOFF:
        if wait:
            print(f"      transient — waiting {wait}s")
            time.sleep(wait)
        try:
            out = classifier(msg)
            break
        except Exception as e:                                  # noqa: BLE001
            last = str(e)
            if not is_transient(last):
                # A real refusal or a malformed response. Recorded as a verdict
                # of "route it", matching triage's own fail-open, and cached
                # because it is a genuine outcome rather than a network event.
                out = {"is_change_request": True,
                       "reason": f"classifier error: {last[:120]}",
                       "model": model, "error": True}
                break
    else:
        raise RateLimited(last or "transport failure")

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return out, False


def load(split):
    path = os.path.join(DATA, f"inbox_{split}.csv")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found. Run: python data/generate_inbox.py")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def as_message(row):
    return T.Message(
        message_id=row["message_id"], from_addr=row["from_addr"],
        subject=row["subject"], body=row["body"], thread_id=row["thread_id"],
        to_addr=row["to_addr"], received_at=float(row["received_at"]),
        headers=json.loads(row["headers"] or "{}"),
        in_reply_to=row["in_reply_to"])


def stratified(rows, cases, limit):
    """
    Proportional across the eleven populations, so a sample cannot accidentally
    drop the hardest one. Deterministic: ordered by message hash, not shuffled,
    so re-running samples the same messages and reuses the same cache.
    """
    if not limit:
        return rows

    def stratum(r):
        if r["is_change_request"] == "True":
            return "corpus:" + cases[r["case_id"]]["action_type"]
        return "noise:" + r["noise_kind"]

    groups = collections.defaultdict(list)
    for r in rows:
        groups[stratum(r)].append(r)

    out = []
    for name in sorted(groups):
        g = sorted(groups[name], key=lambda r: hashlib.sha256(
            r["message_id"].encode()).hexdigest())
        take = max(8, round(limit * len(groups[name]) / len(rows)))
        out.extend(g[:take])
    return out


def score(rows, verdicts, cases):
    """Counts for the model and for the deterministic baseline, side by side."""
    res = {}
    for name in ("classifier", "pre-read"):
        res[name] = dict(tp=0, fp=0, tn=0, fn=0)
    per_stratum = collections.defaultdict(lambda: dict(n=0, model_right=0,
                                                       base_right=0))
    for r in rows:
        truth = r["is_change_request"] == "True"
        msg = as_message(r)
        got = {
            "classifier": bool(verdicts[r["message_id"]]["is_change_request"]),
            "pre-read": T.looks_like_it_touches_money(msg),
        }
        st = ("corpus:" + cases[r["case_id"]]["action_type"] if truth
              else "noise:" + r["noise_kind"])
        per_stratum[st]["n"] += 1
        per_stratum[st]["model_right"] += got["classifier"] == truth
        per_stratum[st]["base_right"] += got["pre-read"] == truth
        for name, g in got.items():
            key = ("tp" if g else "fn") if truth else ("fp" if g else "tn")
            res[name][key] += 1
    return res, per_stratum


def rate(c):
    p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
    r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--limit", type=int, default=400,
                    help="stratified sample size; 0 for the whole inbox")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    rows_all = load(args.split)
    with open(os.path.join(DATA, f"cases_{args.split}.csv"),
              newline="", encoding="utf-8") as f:
        cases = {r["case_id"]: r for r in csv.DictReader(f)}
    vendors = pipeline.load_vendors()

    # Only messages that actually REACH stage 4 are its responsibility. The
    # cheap stages already dropped auto-replies, no-reply senders and unknown
    # senders, and scoring the classifier on messages it never sees would
    # flatter it with work something else did.
    seen, reaching = set(), []
    for r in rows_all:
        res = T.triage(as_message(r), vendors, seen, classifier=None)
        if res.stage == T.S_CLASSIFY:
            reaching.append(r)

    rows = stratified(reaching, cases, args.limit)

    model = args.model
    if model is None:
        model, err = llm_client.detect_model()
        if err:
            raise SystemExit(f"cannot reach the model: {err}")

    print()
    print("=" * 78)
    print(f"TRIAGE CLASSIFIER — {args.split} inbox")
    print("=" * 78)
    print(f"  {len(rows_all)} messages, {len(reaching)} reach stage 4, "
          f"{len(rows)} scored")
    print(f"  model {model}   prompt {T.CLASSIFIER_PROMPT_HASH}")
    print()

    classifier = T.make_classifier(model=model)
    verdicts, fresh = {}, 0
    try:
        for i, r in enumerate(rows, 1):
            out, cached = classify_cached(as_message(r), classifier, model)
            verdicts[r["message_id"]] = out
            if not cached:
                fresh += 1
                llm_client.pace()
            if i % 50 == 0:
                print(f"    {i}/{len(rows)}  ({fresh} new calls)")
    except RateLimited as e:
        print(f"\n  RATE LIMITED after {fresh} new calls: {e}")
        print("  Everything already scored is cached. Re-run to resume.")
        return 1

    res, per_stratum = score(rows, verdicts, cases)

    print()
    print("  Ground truth: should the decision engine see this message?")
    print("  Follow-ups count as YES — R2 checks the 'nothing changed' claim,")
    print("  and R2c catches the mule pattern on exactly such a message.")
    print()
    print(f"  {'':12s} {'prec':>7s} {'recall':>7s} {'F1':>7s} "
          f"{'FN':>5s} {'FP':>5s}")
    for name in ("pre-read", "classifier"):
        p, r, f = rate(res[name])
        c = res[name]
        star = "*" if name == "classifier" else " "
        print(f" {star}{name:12s} {p:7.1%} {r:7.1%} {f:7.1%} "
              f"{c['fn']:5d} {c['fp']:5d}")
    print()
    print("  FN is the one that costs a control: a message the engine never")
    print("  sees. FP costs one extraction and nothing else.")
    print()

    print("  per population   (n, model correct, pre-read correct)")
    for st in sorted(per_stratum):
        d = per_stratum[st]
        print(f"    {st:24s} {d['n']:5d}  {d['model_right'] / d['n']:6.1%}  "
              f"{d['base_right'] / d['n']:6.1%}")
    print()

    errs = sum(1 for v in verdicts.values() if v.get("error"))
    if errs:
        print(f"  {errs} classifier error(s), routed onward by fail-open policy")
        print()

    print("  CAVEAT. The noise is authored, and authored noise is easier than")
    print("  real mail. And the follow-up/chaser boundary is the hardest part")
    print("  of this corpus by construction — which is the point, but it means")
    print("  these numbers describe THIS corpus, not an AP inbox.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
