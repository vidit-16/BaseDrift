"""
BaseDrift — investigator.py

The only genuine agent loop in the system, and the smallest thing in it.

WHAT IT DOES
============
Given a message triage decided to route, it asks the MCP inbox questions about
that message's context — has this sender written before, is there a real
conversation behind this reply, have they moved the destination before — and
turns the answers into Tier 2 signals for the decision engine.

WHY IT IS THE ONE PLACE THAT NEEDS THIS MUCH CARE
=================================================
It is the only component that reads attacker-controlled text WHILE HOLDING
TOOLS. Everywhere else in this system the model is handed a document and asked
what it means; here something with a tool loop is pointed at a mailbox.

Three boundaries, all structural:

  1  EVERY TOOL IS READ-ONLY AND SCOPED TO ONE MAILBOX. Enforced in
     mcp/inbox_server.py, asserted by the tests. An injected "forward this to
     …" has nothing to call.

  2  THE MESSAGE BODY IS AN ARGUMENT, NEVER AN INSTRUCTION. Tool arguments are
     derived from the message's ENVELOPE — sender domain, thread id, timestamp
     — never from its text. A body that says "check the mailbox of VEND0123"
     cannot become a tool call, because nothing reads the body to build one.

  3  THE OUTPUT IS TIER 2 AND CANNOT PASS. Whatever this returns, it can hold a
     payout and can never release one. That is checked at the door in decide().

WHAT IS AND IS NOT EVALUATED, STATED PLAINLY
============================================
The evidence gathering below is deterministic and is measured by the test
suite. The optional `reasoner` hook — an LLM given the tool descriptors and
allowed to choose calls — is NOT evaluated: there is no eval corpus for it and
no budget to build one, and an unevaluated agent loop is exactly the "coded
capability with nothing exercising it" shape this project keeps finding in
itself. It is wired so it can be evaluated later; until then the deterministic
path is what runs, and the difference is reported rather than blurred.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inbox_signals  # noqa: E402
from decision_engine import Signal  # noqa: E402

MAX_TOOL_CALLS = 8


@dataclass
class Investigation:
    message_id: str
    vendor_id:  Optional[str]
    signals:    List[Signal] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    reasoner_used: bool = False
    notes:      str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "vendor_id": self.vendor_id,
            "signals": [vars(s) for s in self.signals],
            "tool_calls": self.tool_calls,
            "reasoner_used": self.reasoner_used,
            "notes": self.notes,
        }


def investigate(server, message, triage_result,
                is_reply: bool = False,
                reasoner=None) -> Investigation:
    """
    Gather inbox context for one routed message. Never raises.

    The tool-call log is part of the audit record on purpose. "The agent looked
    into it" is not a finding anybody can check; "the agent called
    search_history(balajilogistic.com, before=…) and got nothing back" is.
    """
    inv = Investigation(message_id=message.message_id,
                        vendor_id=getattr(triage_result, "vendor_id", None))

    try:
        signals = inbox_signals.collect(server, message, triage_result,
                                        is_reply=is_reply)
    except Exception as e:                                      # noqa: BLE001
        # Inaction is the safe state. No signals means no inbox evidence, which
        # means the decision falls back to the vendor master and the payout —
        # the inputs that were always authoritative anyway.
        inv.notes = (f"inbox lookup failed ({type(e).__name__}); no inbox "
                     f"evidence contributed. The decision rests on the vendor "
                     f"master and the payout, as it does without a mailbox.")
        return inv

    inv.signals = signals
    inv.tool_calls = list(server.call_log[-MAX_TOOL_CALLS:])

    if reasoner is not None:
        inv.notes = _run_reasoner(server, message, reasoner, inv)
        inv.reasoner_used = True

    # Belt and braces: the same property decide() checks at the door, checked
    # again here so a bad signal is caught where it was produced.
    inbox_signals.assert_cannot_release(inv.signals)
    return inv


def _run_reasoner(server, message, reasoner, inv) -> str:
    """
    The optional model loop. UNEVALUATED — see the module docstring.

    It may add NOTES and nothing else. It cannot add a signal, because a model
    reading attacker-controlled text must not be able to author evidence that
    reaches the rule table; the signals above are derived from tool results by
    code. The worst a hostile message can do here is make the note misleading,
    which no rule reads.
    """
    try:
        out = reasoner(message, server)
    except Exception as e:                                      # noqa: BLE001
        return f"reasoner failed ({type(e).__name__}); no note recorded"
    if not isinstance(out, str):
        return "reasoner returned a non-text response; discarded"
    return out[:1000]
