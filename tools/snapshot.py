"""
BaseDrift — freeze the operator dashboard into a folder of static HTML.

    python src/demo.py --serve          # in one terminal
    python tools/snapshot.py            # in another

WHY THIS EXISTS RATHER THAN A TUNNEL
====================================
The obvious way to let somebody else look at the dashboard is to expose the
running app — ngrok, a Cloudflare tunnel, a deployed box. Don't. This app has
four POST routes and none of them has authentication:

    POST /documents            files a change request for any vendor
    POST /messages             injects a message into triage
    POST /case/{id}/action     records verification outcomes, releases payouts
    POST /webhooks/razorpay    HMAC-verified, the one that is actually guarded

Tunnel URLs get scanned in minutes. The synthetic data means a leak costs
nothing, but anyone POSTing to /case/.../action while somebody is presenting
corrupts the demo live, and that is an unforced error.

A static snapshot has no POST routes at all. The forms are still drawn, because
the buttons are half the story — they are simply inert, which is the correct
behaviour for a page anyone can open.

WHAT IT PRESERVES
The dashboard is server-rendered HTML with inlined CSS and no JavaScript, so a
snapshot is byte-for-byte what the live app served, minus the rewritten links.
Nothing is re-implemented here and nothing can drift: if this script had to
reconstruct the pages, the snapshot would be a second renderer to keep in sync.

WHAT IT DELIBERATELY LOSES
Recording a callback and being refused the release is the one thing that has to
be shown live, because it is a state change. That is the right split: the
snapshot is for people browsing on their own device, the running app is for the
part you narrate.
"""

import html
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASEDRIFT_SNAPSHOT_BASE", "http://127.0.0.1:8000")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")

BANNER = (
    '<div class="snapshot-note">'
    "<strong>Static snapshot.</strong> A frozen copy of the operator dashboard, "
    "safe to open anywhere. The buttons below are drawn but inert — recording a "
    "verification and being refused the release is a state change, so it runs on "
    "the live app: <code>python src/demo.py --serve</code>."
    "</div>"
)

BANNER_CSS = """
.snapshot-note{border:1px solid var(--accent);background:rgba(79,189,180,.08);
  border-radius:6px;padding:12px 16px;margin-bottom:18px;font-size:13px;
  color:var(--dim)}
.snapshot-note strong{color:var(--ink)}
.snapshot-note code{color:var(--accent)}
form[data-inert] .btn,form[data-inert] select,form[data-inert] textarea{
  opacity:.55;cursor:not-allowed}
"""


def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.read().decode("utf-8")


def slug(url_path):
    """A filename for a route. Message ids carry @ and <> and must survive."""
    raw = urllib.parse.unquote(url_path)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")
    return (safe or "page")[:120]


def local_name(href):
    """Where a live route lands on disk, or None if it is not a page we keep."""
    if href in ("/", ""):
        return "index.html"
    if href == "/inbox":
        return "inbox.html"
    if href.startswith("/message/"):
        return f"message/{slug(href[len('/message/'):])}.html"
    if href.startswith("/case/"):
        return f"case/{slug(href[len('/case/'):])}.html"
    return None


def rewrite(page, depth):
    """
    Point every link at its file, neuter every form, and say so on the page.

    `depth` is how many directories down this page sits, so links resolve from
    a plain folder — no web server, no base tag, works from file:// too.
    """
    up = "../" * depth

    def href_sub(m):
        target = local_name(m.group(1))
        return f'href="{up}{target}"' if target else m.group(0)

    page = re.sub(r'href="([^"]*)"', href_sub, page)

    # Forms cannot post anywhere. Draw them, disable them, and mark them so the
    # stylesheet can dim the controls rather than silently doing nothing.
    page = re.sub(r'<form method="post" action="[^"]*"',
                  '<form data-inert onsubmit="return false"', page)
    page = page.replace("<button ", '<button disabled ')
    page = page.replace("<select ", '<select disabled ')
    page = page.replace("<textarea ", '<textarea disabled ')

    page = page.replace("</style>", BANNER_CSS + "</style>", 1)
    page = page.replace('<div class="wrap">', '<div class="wrap">' + BANNER, 1)
    return page


def main():
    try:
        inbox = fetch("/inbox")
    except Exception as e:                                     # noqa: BLE001
        print(f"cannot reach {BASE}: {e}\n\nStart it first:\n"
              f"    python src/demo.py --serve")
        return 1

    index = fetch("/")
    routes = {"/": index, "/inbox": inbox}

    found = set(re.findall(r'href="(/message/[^"]+)"', inbox))
    found |= set(re.findall(r'href="(/case/[^"]+)"', inbox + index))
    print(f"{len(found)} linked pages found")

    for href in sorted(found):
        try:
            routes[href] = fetch(href)
        except Exception as e:                                 # noqa: BLE001
            print(f"  skipped {href}: {e}")

    written = 0
    kept = set()
    for href, page in routes.items():
        name = local_name(href)
        if not name:
            continue
        path = os.path.normpath(os.path.join(OUT, name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(rewrite(page, depth=name.count("/")))
        kept.add(path)
        written += 1

    # Remove pages the corpus no longer contains. Writing without pruning is
    # how a snapshot stops being a snapshot: regenerating after the corpus was
    # rescaled left 74 pages from an earlier run sitting in docs/, still
    # committed and still served, describing cases that no longer exist. They
    # are only findable by their content, because nothing links to them.
    #
    # This deletes only .html under docs/, only files this run did not write,
    # and never .nojekyll — and it runs after every page has been written, so
    # a fetch that failed above costs one stale page rather than the folder.
    removed = 0
    for root, _, names in os.walk(OUT):
        for n in names:
            if not n.endswith(".html"):
                continue
            path = os.path.normpath(os.path.join(root, n))
            if path not in kept:
                os.remove(path)
                removed += 1

    # GitHub Pages runs Jekyll by default, which skips files beginning with an
    # underscore. Nothing here starts with one today, and relying on that is a
    # trap for whoever adds a page later.
    with open(os.path.join(OUT, ".nojekyll"), "w") as f:
        f.write("")

    root = os.path.normpath(OUT)
    print(f"\n{written} pages written to {root}")
    if removed:
        print(f"{removed} stale pages removed")
    print("\nHost it any of these ways:")
    print("  GitHub Pages   Settings -> Pages -> main /docs, then open /inbox.html")
    print("  Netlify        drag the docs/ folder onto app.netlify.com/drop")
    print("  Locally        open docs/inbox.html — it works from file:// too")
    return 0


if __name__ == "__main__":
    sys.exit(main())
