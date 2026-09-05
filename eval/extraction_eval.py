"""
BaseDrift — extraction evaluation.

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
from typing import Optional

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
from rules_eval import load_cases, load_vendors, basedrift  # noqa: E402

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


# Transport failures are not extraction results. Conflating them is the same
# category error the decision engine made with WARN — "I could not reach the
# API" says nothing whatsoever about whether the model can read an email, and
# recording it as though it did corrupts the measurement.
TRANSIENT_MARKERS = ("429", "rate limit", "network error", "timed out",
                     "connection", "exhausted retries")

# Sustained limiting needs longer waits than llm_client's per-call retry gives.
TRANSIENT_BACKOFF = (20, 60, 150)


def is_transient(reason: Optional[str]) -> bool:
    r = (reason or "").lower()
    return any(m in r for m in TRANSIENT_MARKERS)


class RateLimited(RuntimeError):
    """Raised when the API is refusing sustained traffic; the run should stop."""


def extract_cached(rendered, model, run):
    """
    Returns (ExtractionResult-as-dict, from_cache).

    A transient failure is retried with escalating backoff and, if it still
    fails, raises rather than being written to disk. An earlier version cached
    whatever came back: a rate-limited run then persisted 201 "extraction
    failed" records that would never be retried and would have silently poisoned
    every future scoring pass with a 56% failure rate that was really the
    network.
    """
    path = cache_path(rendered.sha256, model, run)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True

    res = E.extract(rendered.email, model=model)
    for wait in TRANSIENT_BACKOFF:
        if res.ok or not is_transient(res.failure_reason):
            break
        print(f"      transient ({(res.failure_reason or '')[:48]}...) "
              f"— waiting {wait}s")
        time.sleep(wait)
        res = E.extract(rendered.email, model=model)

    if not res.ok and is_transient(res.failure_reason):
        raise RateLimited(res.failure_reason or "transport failure")

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


def score_fields(cases, extractions, rows_by_id=None):
    """
    Returns (totals, action confusion, per-scenario totals).

    The per-scenario split matters because a headline average hides whether one
    whole narrative is being misread — 96% overall reads fine while every
    ADD_FUND_ACCOUNT case is wrong, and ADD is the distinction the rule table
    treats as materially lower risk.
    """
    f = collections.Counter()
    confusion = collections.Counter()
    by_scen = collections.defaultdict(collections.Counter)
    for c in cases:
        d = extractions[c.case_id]
        exp = c.expected
        scen = (rows_by_id or {}).get(c.case_id, {}).get("scenario_type", "?")
        f["total"] += 1
        by_scen[scen]["total"] += 1
        if not d["ok"]:
            f["extraction_failed"] += 1
            by_scen[scen]["failed"] += 1
            continue
        f["extracted"] += 1
        by_scen[scen]["extracted"] += 1
        s, cl, p = d["semantic"], d["claims"], d["pressure"]

        got = (s["intent"], s["action"], s["scope"])
        want = (exp["intent"], exp["action"], exp["scope"])
        f["semantic_exact"] += got == want
        by_scen[scen]["semantic_exact"] += got == want
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
    return f, confusion, by_scen


def pct(n, d):
    return f"{100*n/d:5.1f}%" if d else "    -"


def scenario_report(by_scen):
    print("  BY SCENARIO — an average can hide one narrative being misread")
    print(f"    {'scenario':22s} {'n':>5s} {'failed':>7s} {'semantics exact':>17s}")
    for scen in sorted(by_scen):
        b = by_scen[scen]
        ex = b["extracted"]
        print(f"    {scen:22s} {b['total']:5d} {b['failed']:7d} "
              f"{b['semantic_exact']:6d}/{ex:<4d} {pct(b['semantic_exact'], ex)}")
    print()


def field_report(f, confusion):
    ex = f["extracted"]
    print("=" * 78)
    print("FIELD-LEVEL RECOVERY")
    print("=" * 78)
    print(f"  cases                  {f['total']}")
    print(f"  extraction failed      {f['extraction_failed']}  "
          f"({pct(f['extraction_failed'], f['total'])})")
    if f["extraction_failed"]:
        print("    (schema or parse failures only — transport failures are")
        print("     retried and never recorded as extraction results)")
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

def compute_outcomes(cases, extractions, rows_by_id, vendors, index):
    """Split from printing so a per-run spread can be taken over these too."""
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
                        destination_account_number=row["proposed_account_number"],
                        vendors=vendors)
        d_ideal = basedrift(row, vendor, index, vendors)

        reached = row["callback_reaches_known_contact"] == "True"
        # The accounts the requester can actually send from. Which one gets
        # DEMANDED is the verifier's decision, not the dataset's — see
        # BUILD-LOG.md V2.S on why the old single bool made that unmeasurable.
        controls = [a for a in
                    (row.get("requester_controls_accounts") or "").split(";") if a]
        for tag, d, ctr in (("real", d_real, real), ("ideal", d_ideal, ideal)):
            v = verifier.verify(d, vendor, reached, c.case_id,
                                requester_controls_accounts=controls,
                                as_of=row.get("request_date") or None)
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
    return real, ideal, agree


def rates(c):
    rec = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0
    prec = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0
    legit = c["fp"] + c["tn"]
    return rec, prec, (c["false_block"] / legit if legit else 0)


def outcome_report(cases, extractions, rows_by_id, vendors, index):
    real, ideal, agree = compute_outcomes(cases, extractions, rows_by_id,
                                          vendors, index)
    n = len(cases)
    print("=" * 78)
    print("END TO END — real extraction vs the rules-only upper bound")
    print("=" * 78)
    print(f"  {'':10s} {'recall':>8s} {'prec':>7s} {'falseBLK':>9s} {'same rule':>10s}")
    for tag, c in (("ideal", ideal), ("real", real)):
        rec, prec, fb = rates(c)
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
                             f"BASEDRIFT_API_KEY (or GROQ_API_KEY) must be set in "
                             f"THIS shell.")
    print(f"model {model}   scoring {len(cases)} case(s)   runs {args.runs}")
    print()

    per_run = []
    for run in range(1, args.runs + 1):
        extractions, fresh = {}, 0
        try:
            for i, c in enumerate(cases, 1):
                d, cached = extract_cached(c, model, run)
                extractions[c.case_id] = d
                if not cached:
                    fresh += 1
                    llm_client.pace()
                if i % 25 == 0:
                    print(f"    run {run}: {i}/{len(cases)}  ({fresh} new calls)")
        except RateLimited as e:
            # Everything already extracted is cached and keeps. Scoring what we
            # have beats scoring nothing, as long as the count is stated.
            print()
            print(f"  !! stopped at {len(extractions)}/{len(cases)}: {e}")
            print(f"  !! {fresh} new this session; the rest were cached.")
            print("  !! Nothing is lost — re-run later and it resumes.")
            print()
            if not extractions:
                raise SystemExit(1)
            cases = [c for c in cases if c.case_id in extractions]
        f, confusion, by_scen = score_fields(cases, extractions, rows_by_id)
        per_run.append((f, confusion, extractions, by_scen))
        print(f"  run {run} complete — {fresh} new API calls, "
              f"{len(cases)-fresh} from cache")
    print()

    f, confusion, extractions, by_scen = per_run[-1]
    field_report(f, confusion)
    scenario_report(by_scen)
    outcome_report(cases, extractions, rows_by_id, vendors, index)

    if args.runs > 1:
        print("=" * 78)
        print("RUN-TO-RUN SPREAD (the extractor is not deterministic)")
        print("=" * 78)
        print("  Identical inputs, identical temperature. A single pass is one")
        print("  sample; these are the figures quoted above, re-measured.")
        print()
        print(f"  {'field':26s} {'per run':>26s} {'spread':>8s}")
        for label, key in (("semantic exact", "semantic_exact"),
                           ("intent", "intent_ok"),
                           ("action", "action_ok"),
                           ("scope", "scope_ok"),
                           ("account number", "account_ok"),
                           ("GSTIN", "gstin_ok"),
                           ("sender domain", "domain_ok"),
                           ("urgency true-pos", "urgency_tp"),
                           ("channel true-pos", "channel_tp"),
                           ("extraction failed", "extraction_failed")):
            vals = [r[0][key] for r in per_run]
            print(f"  {label:26s} {str(vals):>26s} {max(vals)-min(vals):8d}")
        print()

        print(f"  {'end-to-end':26s} {'per run':>26s} {'spread':>8s}")
        outs = [compute_outcomes(cases, r[2], rows_by_id, vendors, index)
                for r in per_run]
        for i, label in enumerate(("recall", "precision", "false BLOCK")):
            vals = [round(rates(o[0])[i] * 100, 1) for o in outs]
            print(f"  {label:26s} {str(vals):>26s} "
                  f"{max(vals)-min(vals):7.1f}pp")
        agrees = [o[2] for o in outs]
        print(f"  {'same rule as ideal':26s} {str(agrees):>26s} "
              f"{max(agrees)-min(agrees):8d}")
        print()

    print("=" * 78)
    print("PROVENANCE — every figure above belongs with these")
    print("=" * 78)
    scope = ("the FULL split" if len(cases) == len(all_cases)
             else f"a stratified sample of {len(all_cases)}")
    print(f"  cases            {len(cases)}  ({scope}, {args.split})")
    print(f"  runs             {args.runs}")
    print(f"  model            {model}")
    print(f"  prompt hash      {PROMPT_HASH}")
    print(f"  normaliser       v{NORMALIZER_VERSION}")
    print(f"  renderer         {R.RENDERER_VERSION}")
    print(f"  leakage check    {leak:.1%} baseline exact over all "
          f"{len(all_cases)} rendered messages")
    print(f"  measured         {time.strftime('%Y-%m-%d')}")
    print()
    print("  These are EXTRACTION figures. eval/rules_eval.py measures the rule")
    print("  table given perfect evidence; this measures what the model recovers.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
