"""
PayeeProof — extraction evaluation.

eval/rules_eval.py measures the DECISION ENGINE given perfect evidence. This
measures what the extractor actually recovers from prose, which is the gap
between that upper bound and reality. Keeping them separate is deliberate: rule
tuning has to stay instant and deterministic, and this is neither.

WHAT IT REPORTS
  1. Leakage check — the keyword baseline re-run over the rendered corpus. If it
     scores well the renderer reproduced the v1 methodology error and every
     number below is void, so this is printed FIRST and cannot be skipped.
  2. Field-level recovery — semantics, claims, pressure signals.
  3. End-to-end outcomes vs the rules-only upper bound.

NON-DETERMINISM
The extractor is not reproducible run to run even at temperature 0 — six runs of
one input produced three different hedged_fields spellings. A single pass is one
sample. --runs N re-extracts and reports the spread; a bare number from one pass
should not be quoted without the run count beside it.

CACHING
Extractions are cached on disk keyed by (email sha256, model, prompt hash), so
re-scoring costs nothing and only genuinely new work hits the API. The cache is
gitignored: it is derived data, and committing it would let a stale result
outlive the prompt that produced it.

    python eval/extraction_eval.py --limit 80          # stratified sample
    python eval/extraction_eval.py --runs 3 --limit 40 # variance
    python eval/extraction_eval.py                     # full dev split
"""

import argparse
import collections
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "data"))
sys.path.insert(0, HERE)

import extractor as E  # noqa: E402
import llm_client  # noqa: E402
import render as R  # noqa: E402
import verifier  # noqa: E402
from ablation import keyword_baseline  # noqa: E402
from decision_engine import ALLOW, BLOCK, STEP_UP, FAVResult, decide  # noqa: E402
from rules_eval import load_cases, load_vendors, payeeproof  # noqa: E402

CACHE_DIR = os.path.join(HERE, ".extraction_cache")
PROMPT_HASH = hashlib.sha256(E.SYSTEM_PROMPT.encode()).hexdigest()[:12]

# Cached entries hold POST-normalisation values, so a change to how claims are
# normalised must invalidate them. The prompt hash alone would not: fixing
# sender_domain normalisation left the prompt untouched and every stale entry
# would have kept its un-normalised value.
NORMALIZER_VERSION = "2"


# ── Cache ─────────────────────────────────────────────────────────────

def cache_path(email_sha, model, run):
    key = (f"{email_sha}_{model}_{PROMPT_HASH}_n{NORMALIZER_VERSION}_r{run}"
           .replace("/", "_"))
    return os.path.join(CACHE_DIR, f"{key}.json")


def extract_cached(rendered, model, run):
    """Returns (ExtractionResult-as-dict, from_cache)."""
    path = cache_path(rendered.sha256, model, run)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    res = E.extract(rendered.email, model=model)
    payload = res.to_dict()
    payload["_meta"] = {
        "model": model, "prompt_hash": PROMPT_HASH,
        "renderer_version": rendered.renderer_version,
        "email_sha256": rendered.sha256, "run": run,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    return payload, False


def result_from_dict(d):
    """Rebuild an ExtractionResult so the decision engine can consume it."""
    if not d["ok"]:
        return E.ExtractionResult(ok=False, failure_reason=d["failure_reason"])
    s, c, p = d["semantic"], d["claims"], d["pressure"]
    return E.ExtractionResult(
        ok=True, intent=s["intent"], action=s["action"], scope=s["scope"],
        reasoning=s["reasoning"],
        proposed_account_number=c["proposed_account_number"],
        proposed_ifsc=c["proposed_ifsc"], proposed_gstin=c["proposed_gstin"],
        sender_domain=c["sender_domain"], sender_phone=c["sender_phone"],
        vendor_name_claimed=c["vendor_name_claimed"], amount=c["amount"],
        urgency_detected=p["urgency_detected"],
        urgency_phrases=p["urgency_phrases"],
        hedging_detected=p["hedging_detected"], hedged_fields=p["hedged_fields"],
        channel_manipulation_detected=p["channel_manipulation_detected"],
        channel_manipulation_phrases=p["channel_manipulation_phrases"],
    )


# ── Sampling ──────────────────────────────────────────────────────────

def stratified(cases, rows_by_id, limit):
    """
    Even coverage across scenarios rather than the first N. A full dev pass is
    ~556 calls at 7s pacing — over an hour — so a sample is the practical
    default, and it must not silently over-represent whichever scenario the
    shuffle happened to front-load.
    """
    if not limit or limit >= len(cases):
        return cases
    buckets = collections.defaultdict(list)
    for c in cases:
        buckets[rows_by_id[c.case_id]["scenario_type"]].append(c)
    out, i = [], 0
    while len(out) < limit:
        added = False
        for k in sorted(buckets):
            if i < len(buckets[k]) and len(out) < limit:
                out.append(buckets[k][i])
                added = True
        if not added:
            break
        i += 1
    return out


# ── Leakage check ─────────────────────────────────────────────────────

def leakage_report(cases):
    exact = intent_ok = 0
    for c in cases:
        got = keyword_baseline(c.email)
        exp = (c.expected["intent"], c.expected["action"], c.expected["scope"])
        exact += got == exp
        intent_ok += got[0] == exp[0]
    n = len(cases)
    print("=" * 78)
    print("LEAKAGE CHECK — keyword baseline over the RENDERED corpus")
    print("=" * 78)
    print(f"  exact (intent+action+scope) : {exact}/{n} = {100*exact/n:5.1f}%")
    print(f"  intent only                 : {intent_ok}/{n} = {100*intent_ok/n:5.1f}%")
    print()
    if exact / n > 0.30:
        print("  *** FAIL: the renderer leaked the baseline's vocabulary. Every")
        print("  *** number below measures word overlap, not understanding. Stop.")
    else:
        print("  PASS. Meaning has to be inferred from these messages, not")
        print("  pattern-matched, so what follows measures the extractor.")
    print()
    return exact / n


# ── Field-level scoring ───────────────────────────────────────────────

def norm(v):
    return None if v is None else str(v).strip().lower()


def score_fields(cases, extractions):
    f = collections.Counter()
    confusion = collections.Counter()
    for c in cases:
        d = extractions[c.case_id]
        exp = c.expected
        f["total"] += 1
        if not d["ok"]:
            f["extraction_failed"] += 1
            continue
        f["extracted"] += 1
        s, cl, p = d["semantic"], d["claims"], d["pressure"]

        got = (s["intent"], s["action"], s["scope"])
        want = (exp["intent"], exp["action"], exp["scope"])
        f["semantic_exact"] += got == want
        f["intent_ok"] += s["intent"] == exp["intent"]
        f["action_ok"] += s["action"] == exp["action"]
        f["scope_ok"] += s["scope"] == exp["scope"]
        if got != want:
            confusion[(want[1], got[1])] += 1

        f["account_ok"] += norm(cl["proposed_account_number"]) == norm(exp["account_number"])
        f["ifsc_ok"] += norm(cl["proposed_ifsc"]) == norm(exp["ifsc"])
        f["gstin_ok"] += norm(cl["proposed_gstin"]) == norm(exp["gstin"])
        f["domain_ok"] += norm(cl["sender_domain"]) == norm(exp["sender_domain"])
        amt = cl["amount"]
        f["amount_ok"] += amt is not None and abs(float(amt) - exp["amount"]) < 1.0

        for name, key in (("urgency", "urgency"),
                          ("channel", "channel_manipulation")):
            got_b = p[f"{'urgency' if name=='urgency' else 'channel_manipulation'}_detected"]
            want_b = exp[key]
            if want_b and got_b:
                f[f"{name}_tp"] += 1
            elif got_b and not want_b:
                f[f"{name}_fp"] += 1
            elif want_b and not got_b:
                f[f"{name}_fn"] += 1
    return f, confusion


def pct(n, d):
    return f"{100*n/d:5.1f}%" if d else "    -"


def field_report(f, confusion):
    ex = f["extracted"]
    print("=" * 78)
    print("FIELD-LEVEL RECOVERY")
    print("=" * 78)
    print(f"  cases                  {f['total']}")
    print(f"  extraction failed      {f['extraction_failed']}  "
          f"({pct(f['extraction_failed'], f['total'])})")
    print()
    print("  SEMANTICS (the thing the ablation says the model is for)")
    for label, key in (("intent", "intent_ok"), ("action", "action_ok"),
                       ("scope", "scope_ok"), ("all three exact", "semantic_exact")):
        print(f"    {label:22s} {f[key]:4d}/{ex:<4d} {pct(f[key], ex)}")
    if confusion:
        print("    misreads (expected -> got):")
        for (want, got), n in confusion.most_common(6):
            print(f"      {want:28s} -> {got:28s} {n}")
    print()
    print("  CLAIMS (never trusted as identity, but must still be recovered)")
    for label, key in (("account number", "account_ok"), ("IFSC", "ifsc_ok"),
                       ("GSTIN", "gstin_ok"), ("sender domain", "domain_ok"),
                       ("amount", "amount_ok")):
        print(f"    {label:22s} {f[key]:4d}/{ex:<4d} {pct(f[key], ex)}")
    print()
    print("  PRESSURE SIGNALS")
    for name in ("urgency", "channel"):
        tp, fp, fn = f[f"{name}_tp"], f[f"{name}_fp"], f[f"{name}_fn"]
        prec = tp / (tp + fp) if tp + fp else 0
        rec = tp / (tp + fn) if tp + fn else 0
        print(f"    {name:22s} precision {prec:5.1%}  recall {rec:5.1%}  "
              f"(tp {tp}, fp {fp}, fn {fn})")
    print()


# ── End-to-end outcomes ───────────────────────────────────────────────

def outcome_report(cases, extractions, rows_by_id, vendors, index):
    real = collections.Counter()
    ideal = collections.Counter()
    agree = 0
    for c in cases:
        row = rows_by_id[c.case_id]
        vendor = vendors[row["vendor_id"]]
        avail = row["fav_name_available"] == "True"
        fav = FAVResult(row["fav_account_status"],
                        row["registered_name_returned"] if avail else None,
                        int(row["name_match_score"]) if avail else None)

        ext = result_from_dict(extractions[c.case_id])
        d_real = decide(ext, fav, vendor, other_vendor_accounts=index,
                        near_duplicate=row["near_duplicate_invoice"] == "True",
                        split_below=row["split_below_threshold"] == "True",
                        destination_account_number=row["proposed_account_number"])
        d_ideal = payeeproof(row, vendor, index)

        reached = row["callback_reaches_known_contact"] == "True"
        for tag, d, ctr in (("real", d_real, real), ("ideal", d_ideal, ideal)):
            v = verifier.verify(d, vendor, reached, c.case_id)
            final = v.final_outcome if v else d.outcome
            allowed = v.payout_allowed if v else d.payout_allowed
            ctr[final] += 1
            if row["label"] == "fraud":
                ctr["tp" if not allowed else "fn"] += 1
            else:
                ctr["fp" if not allowed else "tn"] += 1
                if not allowed and final == BLOCK:
                    ctr["false_block"] += 1
        agree += d_real.rule_fired == d_ideal.rule_fired

    n = len(cases)
    print("=" * 78)
    print("END TO END — real extraction vs the rules-only upper bound")
    print("=" * 78)
    print(f"  {'':10s} {'recall':>8s} {'prec':>7s} {'falseBLK':>9s} {'same rule':>10s}")
    for tag, c in (("ideal", ideal), ("real", real)):
        rec = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0
        prec = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0
        legit = c["fp"] + c["tn"]
        fb = c["false_block"] / legit if legit else 0
        extra = pct(agree, n) if tag == "real" else "     -"
        print(f"  {tag:10s} {rec:8.1%} {prec:7.1%} {fb:9.1%} {extra:>10s}")
    print()
    print("  'ideal' is eval/rules_eval.py's number on these same cases. The gap")
    print("  between the two rows is what the extractor costs.")
    print()


# ── Main ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("dev", "holdout"))
    ap.add_argument("--limit", type=int, default=0, help="stratified sample size")
    ap.add_argument("--runs", type=int, default=1, help="re-extract N times")
    ap.add_argument("--model", default=None, help="pin a model id")
    ap.add_argument("--leakage-only", action="store_true")
    args = ap.parse_args()

    rows = load_cases(args.split)
    rows_by_id = {r["case_id"]: r for r in rows}
    vendors, index = load_vendors()
    all_cases = R.render_split(args.split)

    print()
    print(f"renderer {R.RENDERER_VERSION}   prompt {PROMPT_HASH}   "
          f"split {args.split}   cases {len(all_cases)}")
    print()

    # Always over the FULL corpus: leakage is a property of the renderer, not
    # of whichever subset happens to be sampled.
    leak = leakage_report(all_cases)
    if args.leakage_only:
        return 0 if leak <= 0.30 else 1
    if leak > 0.30:
        return 1

    cases = stratified(all_cases, rows_by_id, args.limit)

    model = args.model
    if model is None:
        model, err = llm_client.detect_model()
        if err:
            raise SystemExit(f"cannot reach the model: {err}\n"
                             f"GROQ_API_KEY must be set in THIS shell.")
    print(f"model {model}   scoring {len(cases)} case(s)   runs {args.runs}")
    print()

    per_run = []
    for run in range(1, args.runs + 1):
        extractions, fresh = {}, 0
        for i, c in enumerate(cases, 1):
            d, cached = extract_cached(c, model, run)
            extractions[c.case_id] = d
            if not cached:
                fresh += 1
                llm_client.pace()
            if i % 25 == 0:
                print(f"    run {run}: {i}/{len(cases)}  ({fresh} new calls)")
        f, confusion = score_fields(cases, extractions)
        per_run.append((f, confusion, extractions))
        print(f"  run {run} complete — {fresh} new API calls, "
              f"{len(cases)-fresh} from cache")
    print()

    f, confusion, extractions = per_run[-1]
    field_report(f, confusion)
    outcome_report(cases, extractions, rows_by_id, vendors, index)

    if args.runs > 1:
        print("=" * 78)
        print("RUN-TO-RUN SPREAD (the extractor is not deterministic)")
        print("=" * 78)
        for label, key in (("semantic exact", "semantic_exact"),
                           ("extraction failed", "extraction_failed")):
            vals = [r[0][key] for r in per_run]
            print(f"  {label:22s} {vals}  spread {max(vals)-min(vals)}")
        print()

    print(f"  Sample: {len(cases)} of {len(all_cases)} {args.split} cases, "
          f"{args.runs} run(s), model {model}.")
    print("  Quote the sample size and run count with any figure above.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
