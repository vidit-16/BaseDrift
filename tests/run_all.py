"""
Every test suite in one command. No API key, no network.

    python tests/run_all.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    "test_decision_engine.py",
    "test_extractor.py",
    "test_verifier_pipeline.py",
    "test_webhook.py",
]


def main():
    failed = []
    for suite in SUITES:
        print(f"\n{'=' * 60}\n{suite}\n{'=' * 60}")
        r = subprocess.run([sys.executable, os.path.join(HERE, suite)])
        if r.returncode:
            failed.append(suite)
    print(f"\n{'=' * 60}")
    print("FAILED: " + ", ".join(failed) if failed else "all suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
