"""
PayeeProof — inbox_signals.py

Turns mailbox facts into Tier 2 signals.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
========================================
INBOX EVIDENCE CAN HOLD A PAYOUT AND CAN NEVER RELEASE ONE.

Not a convention — a property of the values returned. Every signal here is
WARN or INCONCLUSIVE and never PASS, so no combination of them can satisfy a
rule that requires a clean signal. `assert_cannot_release()` checks it
structurally and the test suite calls it.

The reason is the threat model. Inbox history is the most attacker-shapeable
evidence in the system: someone who owns a mailbox can send themselves
messages, build a thread to any depth, and manufacture months of
correspondence. "This sender has written to us fifty times before" is a
sentence an attacker inside the mailbox can make true. Evidence that can be
authored must not be able to say yes.

So a long, established, ordinary-looking correspondence produces NO signal
at all — the absence of a warning, not a reassurance. That asymmetry is the
whole design, and it is why these are Tier 2: contextual, corroborating, never
decisive alone.

WHY NOT MAKE FIRST CONTACT A TIER 1 FAIL
========================================
Because "we have never heard from this sender" is true of every new vendor, of
every vendor whose finance team changed email address, and of every legitimate
rebrand. It is unremarkable in a mailbox and it is not identity evidence. Tier
1 is reserved for comparisons against the vendor master, which is a record the
merchant controls.
"""

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from decision_engine import Signal, WARN, INCONCLUSIVE  # noqa: E402

# A thread that a change request appears in, this shallow, is a message
# presented as a conversation without one having happened.
SHALLOW_THREAD = 2


def first_contact(history: List[Dict[str, Any]]) -> Optional[Signal]:
    """
    Nothing from this sender before now.

    Adverse and weak at the same time, which is exactly Tier 2. It fires on a
    genuine new supplier as readily as on an attacker, so it corroborates and
    never decides.
    """
    if history:
        return None
    return Signal("inbox_first_contact", 2, WARN,
                  "no earlier message from this sender is in the mailbox — a "
                  "first contact asking about a payment destination",
                  "mcp_inbox")


def thread_depth_signal(depth: int, is_reply: bool) -> Optional[Signal]:
    """
    A reply that has nothing to reply to.

    Thread hijacking works because a reply inside a live conversation inherits
    the trust of everything above it. When the message claims to be a reply and
    the mailbox holds no conversation to match, the claim is doing work the
    history does not support.
    """
    if not is_reply:
        return None
    if depth > SHALLOW_THREAD:
        return None
    return Signal("inbox_thread_shallow", 2, WARN,
                  f"presented as a reply, but the mailbox holds {depth} "
                  f"message(s) in this thread — the conversation it borrows "
                  f"credibility from is not there",
                  "mcp_inbox")


# RETIRED: repeat_change_requests(). It counted earlier messages from a sender
# that named a payment destination, and it fired on 70.0% of legitimate change
# requests against 36.8% of fraud — it was measuring how long a relationship had
# existed, because a typosquat has no history at all.
#
# Two attempts to save it, both measured on dev, both recorded rather than
# quietly abandoned:
#
#   1  Count DISTINCT accounts rather than messages. This required fixing the
#      inbox generator, which had been writing a fresh random account into every
#      routine invoice while the template text said "unchanged" and "as held on
#      your file". Worth doing on its own and now done — routine mail names the
#      account on file, so a legitimate sender's history holds exactly one. It
#      did not save the signal: legit 52.7%, fraud 24.9%.
#
#   2  Count prior accounts NOT on file, conditioned on the sender having any
#      history. This finally points the right way — fraud 63.3%, legit 55.4% —
#      and eight points is not a signal, it is noise with a preference.
#
# The ceiling is the corpus, not the threshold. 552 change requests over 90 days
# across 301 domains means 107 of them ask to move the destination twice or more
# in a single quarter; a real supplier does it once in several years. Change
# requests are oversampled ~50x for statistical power, so any "has this sender
# asked before?" signal is measuring the oversampling.
#
# It is not being re-tuned into something unmeasurable. V2.C set the precedent:
# a component that survives only by being unmeasured is the exact defect this
# project keeps finding. The planted-account attack it was meant to catch is
# caught where it was always actually caught — select_verification_account()'s
# seasoning and added_via checks, measured at 12/12 held.
#
# What would bring it back: a corpus with a realistic change-request base rate.
# Nothing about the idea is wrong; the data cannot show it.


def resolution_by_content(match: str) -> Optional[Signal]:
    """
    The sender's address matched nothing; the vendor was found by an identifier
    quoted in the body.

    INCONCLUSIVE rather than WARN, on purpose. It is true of a legitimate
    rebrand and of a forged domain in equal measure, so it says "this could not
    be checked from the address", not "this looks wrong". Per the project rule
    that only WARN may contribute to a rejection, it therefore cannot push a
    case toward one.
    """
    if match != "content":
        return None
    return Signal("inbox_sender_unrecognised", 2, INCONCLUSIVE,
                  "the sender address matches no vendor; the vendor was "
                  "identified only from an identifier quoted in the message, "
                  "which anyone can type",
                  "mcp_inbox")


def collect(server, message, triage_result, is_reply: bool = False) -> List[Signal]:
    """
    Every inbox signal for one message, using the mailbox AS IT WAS when the
    message arrived.

    The `before` cutoff is load-bearing. Without it the agent reads mail that
    arrived after the message under investigation, and "has this sender written
    before?" gets answered with the future — which would make first-contact
    detection look far better than it is.
    """
    domain = message.domain
    before = message.received_at or None

    history = server.search_history(domain, before=before)
    depth = server.thread_depth(message.thread_id)

    # prior_change_requests() is deliberately still an MCP tool and is
    # deliberately no longer called here. The investigation agent may ask the
    # mailbox that question; what was removed is the DETERMINISTIC signal that
    # turned the answer into a hold, because the answer does not discriminate.
    # See the retirement note above.

    signals = [
        first_contact(history),
        thread_depth_signal(depth, is_reply),
        resolution_by_content(triage_result.match if triage_result else ""),
    ]
    return [s for s in signals if s is not None]


def assert_cannot_release(signals: List[Signal]) -> None:
    """
    Structural guard. Called by the tests and cheap enough to call in anger.

    A PASS here would let inbox evidence satisfy a rule that requires a clean
    signal — and inbox evidence is the most attacker-shapeable input the system
    has. This is the assertion that keeps "can hold, never release" true rather
    than merely intended.
    """
    for s in signals:
        assert s.tier == 2, f"{s.name} is Tier {s.tier}; inbox evidence is Tier 2"
        assert s.result in (WARN, INCONCLUSIVE), (
            f"{s.name} returned {s.result}. Inbox evidence must never be able to "
            f"clear anything: a mailbox owner can author their own history.")
