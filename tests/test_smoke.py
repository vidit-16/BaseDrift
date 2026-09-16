"""
BaseDrift — smoke tests: the deployable entry point boots, and the documents
still link to files that exist.

These guard the packaging rather than the policy. The Docker image runs
`uvicorn webhook_app:app`; if that import breaks, every other suite can be
green while the container fails its healthcheck. And the secondary documents
live under docs/ (which GitHub Pages also serves), so a relative link that
was right at the repository root is the easiest thing in this repo to break
silently.

No network, no API key.
"""

import glob
import importlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _fresh_app(env):
    old = {k: os.environ.get(k) for k in [*env, "RAZORPAY_WEBHOOK_SECRET"]}
    os.environ.update(env)
    try:
        sys.modules.pop("webhook_app", None)
        return importlib.import_module("webhook_app")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_entrypoint_boots_and_reports_healthy():
    from fastapi.testclient import TestClient

    mod = _fresh_app({"BASEDRIFT_SEED_DEMO": "0"})
    r = TestClient(mod.app).get("/healthz")
    assert r.status_code == 200, r.text


def test_default_store_starts_empty():
    """An unconfigured deployment must not ship with fixture vendors in it."""
    mod = _fresh_app({"BASEDRIFT_SEED_DEMO": "0"})
    assert type(mod.store).__name__ == "Store"
    assert not getattr(mod.store, "vendors", {}), "store was seeded without the flag"


def test_seeded_demo_boots():
    from fastapi.testclient import TestClient

    mod = _fresh_app({"BASEDRIFT_SEED_DEMO": "1"})
    r = TestClient(mod.app).get("/")
    assert r.status_code == 200, r.text


def test_every_src_module_imports():
    for path in sorted(glob.glob(os.path.join(ROOT, "src", "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name in ("webhook_app",):
            continue
        importlib.import_module(name)


_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def test_relative_markdown_links_resolve():
    # tools/mutate.py runs the suite in a sandbox holding only code and data;
    # there are no documents there to check.
    if not os.path.exists(os.path.join(ROOT, "README.md")):
        return
    docs = [os.path.join(ROOT, "README.md")]
    docs += glob.glob(os.path.join(ROOT, "*.md"))
    docs += glob.glob(os.path.join(ROOT, "docs", "*.md"))
    docs += glob.glob(os.path.join(ROOT, "docs", "notes", "*.md"))
    broken = []
    for doc in sorted(set(docs)):
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in _LINK.findall(text):
            if re.match(r"^[a-z]+:", target):
                continue
            full = os.path.normpath(os.path.join(os.path.dirname(doc), target))
            if not os.path.exists(full):
                broken.append(f"{os.path.relpath(doc, ROOT)} -> {target}")
    assert not broken, "broken links:\n  " + "\n  ".join(broken)


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
