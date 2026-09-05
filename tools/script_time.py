"""
BaseDrift — how long SCRIPT.md actually takes to say.

    python tools/script_time.py
    python tools/script_time.py --wpm 160

WHY THIS EXISTS
===============
The video has a hard five-minute limit, and a script written to timecodes will
happily claim 4:50 while containing seven minutes of words. That is exactly
what happened: the first draft carried section headings reading 0:00 — 0:30
above paragraphs that take fifty seconds to read, and nothing anywhere
disagreed with them.

Timecodes in a document are an intention. This counts the words.

WHAT COUNTS AS SPOKEN
=====================
Exactly one thing: **blockquote lines inside a timed section.** The guide puts
every spoken word in a blockquote under a `**SAY**` label, and everything else
on the page is direction — what is on screen, what to click, where to point,
how to deliver it.

That convention is the reason this file is short. An earlier version tried to
infer speech by excluding headings, quotes, tables and lists, and when the
guide was rewritten to put the words in blockquotes it counted 526 words
instead of 634 and cheerfully reported a minute of headroom that did not exist.
A word counter that silently undercounts is worse than none, because it is
believed.

Bold and italic markers are stripped rather than skipped: "**Both** were always
going to pass" is one spoken sentence.

The pre-flight section and everything from "If it goes wrong" onward are
guidance to the presenter and are never spoken.
"""

import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "SCRIPT.md")

# Sections that are direction, never speech.
NOT_SPOKEN = ("Before you hit record",)

# Everything from here on is guidance to the presenter.
END_MARKERS = ("# If it goes wrong", "# Notes for the take")


def spoken_sections(text):
    """[(section title, spoken words)] — blockquote content only."""
    body = text
    for marker in END_MARKERS:
        if marker in body:
            body = body.split(marker)[0]
    body = re.sub(r"```.*?```", "", body, flags=re.S)

    out, title, buf = [], None, []
    for line in body.splitlines():
        if line.startswith("## "):
            if title is not None:
                out.append((title, buf))
            title, buf = line[3:].strip(), []
            continue
        l = line.strip()
        if not l.startswith(">"):
            continue
        l = l.lstrip(">").strip()
        # A numbered list inside a blockquote is a click instruction.
        if not l or re.match(r"^\d+\.", l):
            continue
        buf.append(re.sub(r"[*_`]", "", l))
    if title is not None:
        out.append((title, buf))
    return [(t, " ".join(b).split()) for t, b in out
            if not any(t.startswith(n) for n in NOT_SPOKEN)]


def stated_window(title):
    """The seconds a section's own heading claims, or None."""
    m = re.match(r"(\d+):(\d\d)\s*[—-]\s*(\d+):(\d\d)", title)
    if not m:
        return None
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return b - a


def mmss(seconds):
    return f"{int(seconds // 60)}:{round(seconds % 60):02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wpm", type=int, default=150,
                    help="speaking rate; 150 is an unhurried natural pace")
    ap.add_argument("--limit", type=float, default=300.0,
                    help="hard limit in seconds (default: a 5-minute video)")
    args = ap.parse_args()

    text = io.open(SCRIPT, encoding="utf-8").read()
    sections = spoken_sections(text)

    total = 0
    over = []
    print(f"\n  spoken content at {args.wpm} wpm\n")
    for title, words in sections:
        secs = len(words) / args.wpm * 60
        total += secs
        want = stated_window(title)
        flag = ""
        if want is not None and secs > want:
            flag = f"   OVER its own heading by {round(secs - want)}s"
            over.append(title)
        print(f"  {len(words):4d} w   {mmss(secs):>5}   {title}{flag}")

    print(f"\n  {sum(len(w) for _, w in sections)} words   "
          f"{mmss(total)} spoken")

    # Clicking, page loads and the two deliberate pauses. Silence is not free
    # and a script that budgets none of it is the one that overruns.
    pauses = 35
    print(f"  + ~{pauses}s for demo interaction and pauses")
    print(f"  = {mmss(total + pauses)} total\n")

    if over:
        print("  Sections longer than the timecode above them:")
        for t in over:
            print(f"    - {t}")
        print()

    if total + pauses > args.limit:
        print(f"  OVER the {mmss(args.limit)} limit by "
              f"{mmss(total + pauses - args.limit)}. Cut words, not pauses.\n")
        return 1
    print(f"  Within {mmss(args.limit)}, with "
          f"{mmss(args.limit - total - pauses)} to spare.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
