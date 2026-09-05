"""
BaseDrift — mutation testing for the invariants this project states out loud.

    python tools/mutate.py              # all mutations
    python tools/mutate.py --list       # what it would run, and why
    python tools/mutate.py --only 3 7   # a subset, by id

WHAT THIS IS FOR
================
A passing test suite says the code does what the tests check. It says nothing
about whether the tests check the things the README claims. Mutation testing
asks the harder question: if I break a stated guarantee, does anything notice?

Every mutation below is a plausible edit — the kind a refactor makes by
accident — that breaks a guarantee this repository asserts in prose. A mutation
that leaves the suite green is the finding: that guarantee is DESCRIBED rather
than ENFORCED, and the prose is writing cheques the tests do not cash.

THIS EXISTS BECAUSE THE ONE-OFF VERSION FOUND SOMETHING THE SUITE MISSED TWICE
=============================================================================
Run as a manual pass, this found a compound-guard blind spot in two separate
places. `decide()` carries `if sig.tier != 2 or sig.result == PASS: raise`, and
every test of it supplied a signal that was BOTH Tier 1 AND PASS — so either
half caught it and NEITHER half was individually exercised. Deleting either
clause left the whole suite green. The identical shape then turned up in
`inbox_signals.assert_cannot_release()`.

That is why mutations 5, 6, 7 and 8 below split each compound guard into its
clauses. A test that trips both halves at once proves the guard exists and
proves nothing about what it is made of.

Being a script rather than a note is the point. A measurement nobody can re-run
is a claim, and this project's recurring finding is that unmeasured components
are where the defects live.

SAFETY
======
It never edits the working tree. The repository is copied to a temporary
directory, mutated there, and the copy is deleted afterwards — so an
interrupted run cannot leave a mutated guard behind in the real source.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories the test suite needs. docs/ is 325 static pages and .git is large;
# neither is read by a test, so copying them would triple the runtime.
COPY = ("src", "tests", "data", "eval", "mcp", "tools")


class Mutation:
    def __init__(self, mid, guarantee, path, old, new):
        self.mid = mid
        self.guarantee = guarantee   # the claim in prose that this breaks
        self.path = path
        self.old = old
        self.new = new


MUTATIONS = [
    # ── The two-person rule ───────────────────────────────────────────
    Mutation(
        1, "Whoever records a verification cannot also release the payment",
        "src/casefile.py",
        "if _who(actor) in verifiers(actions):",
        "if False:"),
    Mutation(
        2, "Nothing releases without a recorded verification",
        "src/casefile.py",
        'if state != "verified":',
        "if False:"),
    Mutation(
        3, "A negative verification outcome is sticky",
        "src/casefile.py",
        'if state == "contested":',
        "if False:"),
    Mutation(
        4, "Identity comparison ignores case and padding, so segregation of "
           "duties cannot be defeated with a shift key",
        "src/casefile.py",
        'return str(actor or "").strip().casefold()',
        'return str(actor or "").strip()'),
    Mutation(
        5, "Only known actions reach the audit trail",
        "src/casefile.py",
        "if action not in ACTIONS:",
        "if False:"),

    # ── Tier separation: inbox evidence can hold, never release ───────
    # 6 and 7 are the two halves of ONE compound guard. Splitting them is the
    # whole point — see the module docstring.
    Mutation(
        6, "decide() refuses a non-Tier-2 inbox signal (clause 1 of 2)",
        "src/decision_engine.py",
        "if sig.tier != 2 or sig.result == PASS:",
        "if sig.result == PASS:"),
    Mutation(
        7, "decide() refuses an inbox signal that PASSes (clause 2 of 2)",
        "src/decision_engine.py",
        "if sig.tier != 2 or sig.result == PASS:",
        "if sig.tier != 2:"),
    Mutation(
        8, "assert_cannot_release() refuses a non-Tier-2 signal (clause 1 of 2)",
        "src/inbox_signals.py",
        "if s.tier != 2:",
        "if False:"),
    Mutation(
        9, "assert_cannot_release() refuses a signal that clears (clause 2 of 2)",
        "src/inbox_signals.py",
        "if s.result not in (WARN, INCONCLUSIVE):",
        "if False:"),

    # ── The rule table's holding rules ────────────────────────────────
    Mutation(
        10, "R5 holds on any Tier 1 WARN or INCONCLUSIVE",
        "src/decision_engine.py",
        "    if t1_unclean:",
        "    if False:"),
    Mutation(
        11, "R6 holds on any Tier 2 WARN or INCONCLUSIVE",
        "src/decision_engine.py",
        "    if t2_unclean:",
        "    if False:"),

    # ── The trust-store anchor, in all three of its forms ─────────────
    # "On file" is not "established". Each clause is mutated separately for the
    # same reason the tier guards are.
    Mutation(
        12, "An account verified only by email is not an anchor",
        "src/decision_engine.py",
        'if a.verified_by not in ("", "unverified"):',
        "if True:"),
    Mutation(
        13, "An account that never settled a payout is not an anchor",
        "src/decision_engine.py",
        "if a.settled_payout_count > 0:",
        "if True:"),
    Mutation(
        14, "The anchor check runs at all",
        "src/decision_engine.py",
        "    a = v.account(dest)\n    if a is None:\n        return None",
        "    a = v.account(dest)\n    if True:\n        return None"),

    # ── The guard on the HTTP path, not just in the library ───────────
    Mutation(
        15, "The release refusal is enforced server-side on POST, not by "
            "greying out a button",
        "src/webhook.py",
        "ok, why = casefile.may_release(actions, actor, final)",
        'ok, why = True, ""'),
]


def build_sandbox():
    """A copy of the repo the mutations are applied to. Never the real tree."""
    tmp = tempfile.mkdtemp(prefix="basedrift-mutate-")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
    for d in COPY:
        src = os.path.join(ROOT, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(tmp, d), ignore=ignore)
    return tmp


def run_suite(sandbox):
    """True when the suite passes. That is what a surviving mutation looks like."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # A key here would make the suite reach the network; every test is designed
    # to run without one, and a mutation run must not start paying for calls.
    for k in ("BASEDRIFT_API_KEY", "GROQ_API_KEY"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, "tests/run_all.py"],
                       cwd=sandbox, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode == 0


def apply(sandbox, m):
    """Returns the original text, or None when the anchor no longer matches."""
    path = os.path.join(sandbox, m.path.replace("/", os.sep))
    with open(path, encoding="utf-8") as f:
        original = f.read()
    if original.count(m.old) != 1:
        return None, original.count(m.old)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(original.replace(m.old, m.new, 1))
    return original, 1


def restore(sandbox, m, original):
    path = os.path.join(sandbox, m.path.replace("/", os.sep))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(original)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", nargs="*", type=int, default=None)
    args = ap.parse_args()

    chosen = [m for m in MUTATIONS
              if args.only is None or m.mid in args.only]

    if args.list:
        for m in chosen:
            print(f"{m.mid:3d}  {m.path}")
            print(f"     breaks: {m.guarantee}")
        return 0

    print(f"{len(chosen)} mutations of stated invariants.")
    print("A SURVIVOR is the finding: the guarantee is described, not enforced.\n")

    sandbox = build_sandbox()
    killed, survived, stale = [], [], []
    try:
        # Baseline first. Mutation results mean nothing if the suite is already
        # red — every mutation would read as "killed" for the wrong reason.
        sys.stdout.write("  baseline (unmutated) ... ")
        sys.stdout.flush()
        if not run_suite(sandbox):
            print("FAIL\n\nThe suite is red before any mutation. Fix that first.")
            return 2
        print("passes\n")

        for m in chosen:
            sys.stdout.write(f"  {m.mid:3d}  {m.path:28s} ")
            sys.stdout.flush()
            original, hits = apply(sandbox, m)
            if original is None:
                print(f"STALE — anchor matched {hits} times, expected 1")
                stale.append(m)
                continue
            try:
                if run_suite(sandbox):
                    print("SURVIVED")
                    survived.append(m)
                else:
                    print("killed")
                    killed.append(m)
            finally:
                restore(sandbox, m, original)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n  {len(killed)}/{len(chosen)} killed")

    if stale:
        print("\nSTALE ANCHORS — this harness has drifted from the source and "
              "is no longer testing what it says:")
        for m in stale:
            print(f"  {m.mid}  {m.path}\n     {m.guarantee}")

    if survived:
        print("\nSURVIVORS — these guarantees are not enforced by any test:")
        for m in survived:
            print(f"  {m.mid}  {m.path}\n     {m.guarantee}")

    return 1 if (survived or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
