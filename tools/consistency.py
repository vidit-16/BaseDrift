"""
BaseDrift — do the documents agree with each other, and with the repository?

    python tools/consistency.py
    python tools/consistency.py --list      # what it knows how to check

WHY THIS EXISTS
===============
This repository's most persistent defect is not a bug. It is a number that was
true when it was written and was never re-checked.

Ten of them accumulated at once: the README quoted a 278-case holdout at 87.7%
precision for a corpus that had been regenerated into 276 cases at 87.4%. The
snapshot page count said 205 when it was 325. The inbox said 8,086 messages
when it held 25,584. eval/base_rates.py printed "800 cases" directly above rows
summing to 900. Every one of them was correct on the day it was typed.

Nothing caught any of it, because prose is not executable and nobody diffs a
paragraph against a CSV. Splitting the README into six documents made the
exposure worse, not better: the same figure now appears in several files, and
a corpus regeneration silently invalidates all of them at once.

So the figures are checked the way the code is.

WHAT IT CHECKS
==============
1. DERIVED FACTS — claims that can be recomputed from the repository right now:
   corpus sizes, vendor and account counts, inbox composition, test count,
   snapshot page count, the extractor prompt hash. If a document states one of
   these, it must match what the files actually contain.

2. AGREEMENT — figures that cannot be derived without running an eval
   (precision, recall, hold rates). These are not verified against ground
   truth here; what is verified is that every document states the SAME value.
   Two files disagreeing is a defect regardless of which is right.

WHAT IT DELIBERATELY IGNORES
============================
BUILD-LOG.md is an append-only working log. Its older entries quote figures
that were true when measured and are explicitly marked historical — that is the
point of a log, and rewriting them would make it worthless. It is excluded
wholesale, and the entry that carries superseded numbers says so in its own
header.

Code fences are skipped too: a sample of terminal output is a transcript, not a
claim, and the numbers in it belong to the run that produced them.
"""

import argparse
import csv
import glob
import hashlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))

# Append-only history; see the module docstring.
EXCLUDE = {"BUILD-LOG.md"}


def docs():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "*.md"))
                    + glob.glob(os.path.join(ROOT, "notes", "*.md"))):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if os.path.basename(rel) in EXCLUDE:
            continue
        out.append(rel)
    return out


# A figure that no longer describes the repository is permitted only where the
# sentence says so. "216 tests" is a false claim; "216 tests at the time" is
# history. Requiring the marker is the point — it forces the prose to date
# itself rather than quietly going stale.
HISTORICAL = re.compile(
    r"\bat the time\b|\bthen in the suite\b|\bv1\b|\bhistorical\b|"
    r"\bwas true when\b|\bas it stood\b|\bback then\b",
    re.I)


def strip_fences(text):
    """
    Blank out everything that is not a live claim, preserving line numbers so
    reported locations stay accurate.

    Three things are not live claims: code fences (a transcript belongs to the
    run that produced it), <details> blocks (used here only for explicitly
    labelled v1 comparisons), and any line that dates itself.
    """
    lines = text.split("\n")
    fence = details = False
    for i, l in enumerate(lines):
        low = l.lstrip().lower()
        if low.startswith("```"):
            fence = not fence
            lines[i] = ""
            continue
        if low.startswith("<details"):
            details = True
        if fence or details or HISTORICAL.search(l):
            lines[i] = ""
        if low.startswith("</details"):
            details = False
    return lines


# ── 1. Facts derived from the repository itself ──────────────────────

def derived():
    def rows(p):
        with io.open(os.path.join(ROOT, p), encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    inbox = rows("data/inbox_dev.csv")
    cr = sum(1 for r in inbox if r.get("is_change_request") == "True")

    sys.path.insert(0, os.path.join(ROOT, "src"))
    import extractor  # noqa: E402
    prompt = hashlib.sha256(extractor.SYSTEM_PROMPT.encode()).hexdigest()[:12]

    tests = sum(
        1 for f in glob.glob(os.path.join(ROOT, "tests", "test_*.py"))
        for line in io.open(f, encoding="utf-8") if line.startswith("def test_"))

    return {
        "dev split":      (len(rows("data/cases_dev.csv")),
                           r"\bdev \((\d{3})\)|\b(\d{3}) labeled cases\b"),
        "holdout split":  (len(rows("data/cases_holdout.csv")),
                           r"holdout[, ]+(\d{3}) cases|\bholdout \((\d{3})\)|"
                           r"\b(\d{3}) cases, the full split"),
        "vendors":        (len(rows("data/vendor_master.csv")),
                           r"\b(\d+) (?:synthetic )?vendors\b"),
        "accounts":       (len(rows("data/vendor_accounts.csv")),
                           r"\b(\d+) accounts with provenance\b"),
        # Deliberately broad. The first version matched only the README's exact
        # phrasing and sailed straight past "7,176 in the corpus" sitting stale
        # in three other files.
        "inbox messages": (len(inbox),
                           r"\b([\d,]{4,7}) (?:synthetic )?(?:messages|rows)\b|"
                           r"serves — ([\d,]{4,7}) in the corpus"),
        "tests":          (tests, r"\b(\d+) tests\b"),
        "snapshot pages": (len(glob.glob(os.path.join(ROOT, "docs", "**", "*.html"),
                                         recursive=True)),
                           r"\b(\d+) pages\b"),
        "prompt hash":    (prompt, r"[Pp]rompt (?:hash )?`?([0-9a-f]{12})`?"),
    }


# ── 2. Figures that must simply agree everywhere ─────────────────────
#
# Each is a concept and the pattern that captures whatever a document claims
# for it. The checker does not know the right answer; it knows they must match.

AGREE = {
    "holdout precision":   r"precision (?:is )?\*?\*?(8[0-9]\.[0-9])%\*?\*?[^|\n]*holdout|"
                           r"holdout[^|\n]*?\b(8[0-9]\.[0-9])% precision",
    "null precision":      r"null baseline of [\d]+% / ([\d.]+)%",
    "mutations":           r"\b(\d+)\s*/\s*(\d+)\s*killed|\b(\d+)/(\d+)\*?\*? killed",
    "ablation baseline":   r"\b0\s*/\s*(14)\b",
    "ablation model":      r"\b(14)\s*/\s*14\b",
    "planted-account dip": r"recall (?:fell|dropped) to \*?\*?(9[0-9]\.[0-9])%",
    "test suites":         r"\b(\d+) suites\b",
}


def scan(pattern, lines):
    """[(line number, captured value)] for every match outside a code fence."""
    hits = []
    for n, line in enumerate(lines, 1):
        for m in re.finditer(pattern, line):
            val = next((g for g in m.groups() if g), None)
            if val is not None:
                hits.append((n, val.replace(",", "")))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    facts = derived()
    if args.list:
        print("\n  derived from the repository:")
        for k, (v, _) in facts.items():
            print(f"    {k:18s} = {v}")
        print("\n  checked only for agreement between documents:")
        for k in AGREE:
            print(f"    {k}")
        return 0

    files = {f: strip_fences(io.open(os.path.join(ROOT, f), encoding="utf-8").read())
             for f in docs()}
    print(f"\n  {len(files)} documents checked "
          f"({', '.join(sorted(EXCLUDE))} excluded — append-only history)\n")

    problems = []

    for name, (truth, pattern) in facts.items():
        for f, lines in files.items():
            for n, got in scan(pattern, lines):
                if got != str(truth):
                    problems.append(
                        f"  {f}:{n}  {name}: document says {got}, "
                        f"repository says {truth}")

    for name, pattern in AGREE.items():
        claims = {}
        for f, lines in files.items():
            for n, got in scan(pattern, lines):
                claims.setdefault(got, []).append(f"{f}:{n}")
        if len(claims) > 1:
            problems.append(f"  {name}: documents disagree —")
            for val, where in sorted(claims.items()):
                problems.append(f"      {val}  in {', '.join(where)}")

    if problems:
        print("\n".join(problems))
        print(f"\n  {len(problems)} problem(s)\n")
        return 1

    print("  every derived figure matches the repository, and no two documents\n"
          "  state different values for the same figure.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
