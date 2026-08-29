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


def repeat_change_requests(priors: List[Dict[str, Any]]) -> Optional[Signal]:
    """
    This sender has asked about a payment destination before.

    Deliberately NOT scored as reassuring, which is the tempting reading — "they
    have done this before, it is routine". A sender who has repeatedly moved the
    destination is the pattern behind the planted-account attack: the first
    request gets an account onto the master and the second uses it. So the
    signal fires on repetition and stays silent on a single prior.
    """
    if len(priors) < 2:
        return None
    return Signal("inbox_repeat_destination_requests", 2, WARN,
                  f"{len(priors)} earlier message(s) from this sender also named "
                  f"a payment destination; a sender who moves it repeatedly is "
                  f"the shape of a foothold being reused",
                  "mcp_inbox")


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
    priors = server.prior_change_requests(domain, before=before)
    depth = server.thread_depth(message.thread_id)

    signals = [
        first_contact(history),
        thread_depth_signal(depth, is_reply),
        repeat_change_requests(priors),
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
