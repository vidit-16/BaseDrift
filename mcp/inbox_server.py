"""
BaseDrift — MCP inbox server.

Read-only tools over one merchant's accounts-payable mailbox. Two callers:

  1  triage, which pulls messages in;
  2  the investigation agent, which asks questions ABOUT a message it has been
     handed — how deep the thread is, whether this sender has ever written
     before, what they asked for last time.

THE TWO CONSTRAINTS, BOTH ENFORCED IN CODE RATHER THAN DOCUMENTED
=================================================================

EVERY TOOL IS READ-ONLY. There is no send, no reply, no label, no delete, no
mark-as-read. This is not an oversight to be filled in later: the agent that
calls these tools reads attacker-controlled text, so a write tool is a way for
that text to act. The one place a prompt injection could reach an outbound
action is a tool that has one, and there isn't one.

EVERY TOOL IS SCOPED TO ONE MAILBOX. The server is constructed with a merchant
id and refuses anything outside it. A tool that takes a mailbox parameter is a
tool an injected instruction can point somewhere else.

WHAT THESE TOOLS CANNOT DO, WHICH IS THE MORE IMPORTANT HALF
============================================================
Nothing here returns a verdict, a score, or a recommendation. They return
FACTS ABOUT A MAILBOX: counts, dates, thread depth, previous subjects. The
signals derived from those facts are Tier 2 — see inbox_signals.py — which
means they can hold a payout and can never release one.

That ordering matters. Inbox history is the most attacker-shapeable evidence in
the system: someone inside a mailbox can send themselves messages, build thread
depth, and manufacture a correspondence history. Evidence an attacker can
author must never be able to say yes.

TRANSPORT
=========
The tool functions are the contract. `TOOLS` describes them in MCP's shape so a
stdio or HTTP server is a thin wrapper, and everything here is exercised
directly by the tests without a transport running.
"""

import csv
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")


class ScopeError(PermissionError):
    """A tool call that reached outside the merchant's own mailbox."""


@dataclass
class StoredMessage:
    message_id: str
    thread_id: str
    from_addr: str
    to_addr: str
    subject: str
    body: str
    received_at: float
    in_reply_to: str = ""

    @property
    def domain(self) -> str:
        return self.from_addr.split("@")[-1].lower() if "@" in self.from_addr else ""

    def public(self) -> Dict[str, Any]:
        """
        What a tool returns. The body is included because the agent has to be
        able to read the message it is reasoning about — but note what is NOT
        here: no ground truth, no case id, no scenario, no label. The mailbox
        does not know which of its messages are fraudulent, and neither can
        anything reading it.
        """
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "from": self.from_addr,
            "subject": self.subject,
            "body": self.body,
            "received_at": self.received_at,
            "in_reply_to": self.in_reply_to,
        }


class InboxServer:
    """
    One merchant's mailbox. Constructed with the merchant id; every tool checks
    it. There is deliberately no way to widen the scope after construction.
    """

    def __init__(self, merchant_id: str, messages: List[StoredMessage]):
        self.merchant_id = merchant_id
        self._messages = sorted(messages, key=lambda m: m.received_at)
        self._by_id = {m.message_id: m for m in self._messages}
        self._by_thread: Dict[str, List[StoredMessage]] = {}
        for m in self._messages:
            self._by_thread.setdefault(m.thread_id, []).append(m)
        self.call_log: List[Dict[str, Any]] = []

    # -- scope ---------------------------------------------------------

    def _check(self, merchant_id: Optional[str], tool: str, **kw) -> None:
        if merchant_id is not None and merchant_id != self.merchant_id:
            raise ScopeError(
                f"{tool}: this server serves {self.merchant_id!r} only. A tool "
                f"that could be pointed at another merchant's mailbox is a tool "
                f"an injected instruction can point there.")
        self.call_log.append({"tool": tool, **kw})

    # -- tools ---------------------------------------------------------

    def get_message(self, message_id: str,
                    merchant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        self._check(merchant_id, "get_message", message_id=message_id)
        m = self._by_id.get(message_id)
        return m.public() if m else None

    def get_thread(self, thread_id: str,
                   merchant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every message in one conversation, oldest first."""
        self._check(merchant_id, "get_thread", thread_id=thread_id)
        return [m.public() for m in self._by_thread.get(thread_id, [])]

    def search_history(self, domain: str, before: Optional[float] = None,
                       limit: int = 50,
                       merchant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Everything this sender domain has ever written, oldest first.

        `before` exists so the agent sees the mailbox AS IT WAS when the message
        under investigation arrived. Without it the agent reads the future, and
        "has this sender written before?" would be answered with mail that
        arrived afterwards — a leak that makes first-contact detection look far
        better than it is.
        """
        self._check(merchant_id, "search_history", domain=domain, before=before)
        d = (domain or "").strip().lower()
        out = [m for m in self._messages
               if m.domain == d and (before is None or m.received_at < before)]
        return [m.public() for m in out[:limit]]

    def prior_change_requests(self, domain: str, before: Optional[float] = None,
                              merchant_id: Optional[str] = None
                              ) -> List[Dict[str, Any]]:
        """
        Earlier messages from this sender that named a payment destination.

        Uses triage's own structural pre-read rather than a second opinion, so
        "what counts as touching money" has one definition in this codebase.
        """
        self._check(merchant_id, "prior_change_requests", domain=domain,
                    before=before)
        import sys
        sys.path.insert(0, os.path.join(HERE, "..", "src"))
        import triage as T

        out = []
        for m in self._messages:
            if m.domain != (domain or "").strip().lower():
                continue
            if before is not None and m.received_at >= before:
                continue
            probe = T.Message(message_id=m.message_id, from_addr=m.from_addr,
                              subject=m.subject, body=m.body)
            if T.looks_like_it_touches_money(probe):
                out.append(m.public())
        return out

    def thread_depth(self, thread_id: str,
                     merchant_id: Optional[str] = None) -> int:
        self._check(merchant_id, "thread_depth", thread_id=thread_id)
        return len(self._by_thread.get(thread_id, []))


# ── MCP tool descriptors ──────────────────────────────────────────────
# The shape a stdio or HTTP server would advertise. Every one is read-only;
# there is no write tool to advertise.

TOOLS = [
    {"name": "get_message",
     "description": "Fetch one message by id from this merchant's AP mailbox.",
     "readOnly": True,
     "inputSchema": {"type": "object",
                     "properties": {"message_id": {"type": "string"}},
                     "required": ["message_id"]}},
    {"name": "get_thread",
     "description": "Every message in one conversation, oldest first.",
     "readOnly": True,
     "inputSchema": {"type": "object",
                     "properties": {"thread_id": {"type": "string"}},
                     "required": ["thread_id"]}},
    {"name": "search_history",
     "description": ("Messages previously received from a sender domain. Pass "
                     "`before` to see the mailbox as it was at that time."),
     "readOnly": True,
     "inputSchema": {"type": "object",
                     "properties": {"domain": {"type": "string"},
                                    "before": {"type": "number"},
                                    "limit": {"type": "integer"}},
                     "required": ["domain"]}},
    {"name": "prior_change_requests",
     "description": ("Earlier messages from a sender that named a payment "
                     "destination."),
     "readOnly": True,
     "inputSchema": {"type": "object",
                     "properties": {"domain": {"type": "string"},
                                    "before": {"type": "number"}},
                     "required": ["domain"]}},
    {"name": "thread_depth",
     "description": "How many messages are in a conversation.",
     "readOnly": True,
     "inputSchema": {"type": "object",
                     "properties": {"thread_id": {"type": "string"}},
                     "required": ["thread_id"]}},
]


def assert_read_only() -> None:
    """
    Structural guard, called by the tests. A write tool added here fails the
    build rather than being noticed in review — the agent calling these tools
    reads attacker-controlled text, and the only thing keeping an injection
    from acting is that there is nothing to act with.
    """
    forbidden = ("send", "reply", "delete", "trash", "label", "move", "mark",
                 "archive", "forward", "draft", "write", "update", "create")
    for tool in TOOLS:
        assert tool.get("readOnly") is True, f"{tool['name']} is not read-only"
        for word in forbidden:
            assert word not in tool["name"].lower(), \
                f"{tool['name']} looks like a write tool"
    for name in dir(InboxServer):
        if name.startswith("_"):
            continue
        for word in forbidden:
            assert word not in name.lower(), f"InboxServer.{name} looks like a write"


# ── Loading the synthetic mailbox ─────────────────────────────────────

def from_csv(split: str = "dev", merchant_id: str = "MERCH0001") -> InboxServer:
    path = os.path.join(DATA, f"inbox_{split}.csv")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found. Run: python data/generate_inbox.py")
    messages = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            messages.append(StoredMessage(
                message_id=r["message_id"], thread_id=r["thread_id"],
                from_addr=r["from_addr"], to_addr=r["to_addr"],
                subject=r["subject"], body=r["body"],
                received_at=float(r["received_at"]),
                in_reply_to=r["in_reply_to"],
            ))
    return InboxServer(merchant_id, messages)


if __name__ == "__main__":
    server = from_csv()
    assert_read_only()
    print(f"inbox for {server.merchant_id}: {len(server._messages)} messages, "
          f"{len(server._by_thread)} threads")
    print(f"tools: {[t['name'] for t in TOOLS]}  (all read-only)")
    print()
    print(json.dumps(TOOLS[2], indent=2))
