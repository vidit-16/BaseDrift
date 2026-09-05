# MCP — where it stands, and what to decide

Written to be picked up cold. Everything below came out of working through what
`mcp/inbox_server.py` actually is, and it ends in a decision that has not been
made.

**[← back to the project](../README.md)** · **[other working notes](README.md)**

---

**Short version:** the MCP layer is a well-shaped seam with nothing plugged into
it. That is defensible if described accurately and indefensible if oversold.

---

## 1. What MCP is, in four parts

The specification has four pieces. Most write-ups list three and use a product
word for one of them.

| part | what it is | in BaseDrift |
|---|---|---|
| **Host** | the AI application | `src/investigator.py` |
| **Client** | lives in the host, holds one connection to one server | **missing** |
| **Server** | exposes data and tools | `mcp/inbox_server.py` |
| **Transport** | how the bytes move | **missing** |

**"Connector" is not a spec term.** It is Anthropic's product word for a
packaged, ready-to-enable MCP server in the Claude apps. When an article says a
connector "links the host to the server", it is describing what the spec calls
the **client**.

### Transport, concretely

Two standard choices, carrying identical JSON-RPC messages:

- **stdio** — the host launches the server as a subprocess and they talk over
  stdin/stdout. Right choice for BaseDrift: runs locally, nothing on a network.
- **Streamable HTTP** — the server is a web service. What you would use if
  Razorpay hosted one inbox server for many merchants.

### The three primitives — it is *not* all function calling

| primitive | who decides to use it | example |
|---|---|---|
| **Tools** | the **model** | `search_history(domain)` |
| **Resources** | the **application** | a document at a URI, attached like a file |
| **Prompts** | the **user** | a saved template picked from a menu |

Only **tools** are function calling. Resources are data the app pulls in;
prompts are canned instructions a person chooses.

---

## 2. What is actually built

`mcp/inbox_server.py` — five read-only tools with proper MCP descriptors:

    get_message, get_thread, search_history, prior_change_requests, thread_depth

Three properties are real, enforced in code, and asserted by tests:

- **Every tool is read-only.** No send, reply, delete, label. A test greps for
  write verbs and fails the build if one appears. This matters because the agent
  reads attacker-controlled text while holding tools; the only thing stopping an
  injected "forward this to…" is that there is nothing to forward with.
- **Every tool is scoped to one merchant** and refuses anything else.
- **Tool arguments come from the envelope** — sender domain, thread id,
  timestamp — never from the message body. A test proves a hostile body produces
  byte-identical tool calls to a benign one.
- **`search_history` takes a `before` cutoff**, so the mailbox is read as it was
  when the message arrived rather than including mail that came later.

### What is NOT built

- **No client, no transport.** Nothing external can connect. The agent calls the
  Python methods directly, in-process.
- **No real mailbox.** `from_csv()` reads `data/inbox_dev.csv` — 25,584 synthetic
  messages from `data/generate_inbox.py`. There is no Gmail or Microsoft 365
  connection, broken or otherwise.
- **No model chooses anything.** `inbox_signals.collect()` calls the same three
  tools, in the same order, on every message.

### Fixed since this file was written: the layer was not connected

At the time of writing, `webhook.py` and `pipeline.py` never passed
`inbox_signals` to `decide()`. The two halves of the system ran without
touching: the payout path decided without inbox evidence, and the inbox path
produced signals nothing consumed. `decide()` accepted the argument and no
caller in the live path supplied it — a capability with nothing behind it, at
the integration level rather than in the data.

Now wired. `POST /messages` is the front door: it takes a RAW message with no
vendor id, triage resolves the vendor with no model call, and only messages
reaching ROUTE become documents. Inbox signals are gathered **at arrival** and
stored on the document, so the payout decision days later reads the mailbox as
it stood when the message came in — asking at payout time would count mail that
landed in between and make a first contact look established. Six tests cover it.

**The reported holdout figures do not include inbox evidence**, and that is
deliberate rather than an oversight: `rules_eval` calls `decide()` directly on
the case corpus, which has no mailbox context. Because every inbox signal is
Tier 2 and can only be WARN or INCONCLUSIVE, adding them can only turn an ALLOW
into a hold — never the reverse. So the true recall cannot be worse than
reported and the true hold rate can only be higher. Measuring it properly means
scoring the holdout a third time and was not worth it.

---

## 3. The worked example, for reference

A real fraud case from the dev corpus. Vendor `VEND0079`, Anand Pvt Ltd, real
domain `anandpvtltd.com`, 50 genuine messages in the mailbox.

    From:    payments@anandpvtltd-billing.com     <- forged lookalike
    Subject: INV-4378

    The facility you have on file was with our previous banking partner,
    whose relationship with us ended last week. Everything reaches
    133688561858 (ICIC0205061) from here.

Triage resolved it to `VEND0079` by **content match** (the body quotes the real
GSTIN) — a domain allowlist would have dropped it, because the forged domain is
too far from the real one to register as a typosquat.

The three tool calls, from the audit record:

```json
{"tool": "search_history",        "domain": "anandpvtltd-billing.com", "before": 1779839613}
{"tool": "prior_change_requests", "domain": "anandpvtltd-billing.com", "before": 1779839613}
{"tool": "thread_depth",          "thread_id": "THR00593"}
```

| question | answer |
|---|---|
| messages from `anandpvtltd-billing.com` before this one | **0** |
| messages from the real `anandpvtltd.com` | **50** |

Signals produced, both Tier 2 — they can hold a payout and can never release
one:

- `inbox_first_contact` — WARN
- `inbox_sender_unrecognised` — INCONCLUSIVE

---

## 4. The honest problem

**For a mandatory, deterministic step, MCP buys nothing over a plain module.**

MCP's headline feature is a model dynamically selecting tools. That is switched
off here on purpose — a payment control should not have its evidence gathering
depend on what the model felt like looking up. Same message, same investigation,
every time.

So the question "if the call is mandatory anyway, why MCP?" is fair, and the
answer for the code as it stands is: **it is not needed.** Three plain functions
would do the same work with less machinery.

MCP is also **not the only way** to read a real mailbox. The Gmail API or
Microsoft Graph work fine from ordinary Python.

---

## 5. What would actually justify it

Ranked by how much weight each can carry.

**1. Credentials — the real argument.**
A direct Gmail integration means BaseDrift holds OAuth tokens for the
merchant's mailbox: a fraud vendor with read access to customer email. That is a
serious liability and COMPLIANCE.md already has to argue data minimisation under
DPDP.

With MCP, **the merchant runs the server.** They hold the credentials, they set
the scopes, they can audit what was called. BaseDrift only ever names a tool.
That is an architectural difference, not a stylistic one, and it is the argument
worth keeping.

**2. The integration comes for free.**
Google Workspace and Microsoft 365 MCP servers already exist. Speaking MCP means
a real mailbox works without writing or maintaining mail-provider code.

**A caveat that belongs with argument 1.** Today the read-only guarantee is
STRUCTURAL: there is no send function in `mcp/inbox_server.py`, and a test greps
for write verbs and fails the build if one appears. An injection cannot call a
write tool because none exists.

Swap to a real mailbox server and that weakens to a CONFIGURATION. The write
tools exist; you are relying on having disabled them, and on nobody enabling one
later "just for notifications". Same words — "read-only" — much weaker promise.
By construction versus by settings. Whatever is chosen, say which one it is.

**3. One agent, many backends.**
CSV in testing, Gmail at one merchant, Outlook at another, agent untouched. Real,
but weak on its own — a plain Python interface gives the same thing.

---

## 6. How to describe it (until the transport exists)

**Defensible:**
> The inbox sits behind a read-only, single-tenant tool boundary shaped as MCP,
> so a real mailbox can be plugged in without the merchant handing us their mail
> credentials.

**Avoid:**
> BaseDrift uses MCP to read inboxes.

Anyone who knows MCP will ask which transport, and there isn't one.

---

## 7. The decision, not yet made

**A — build the transport.** ~1 hour. Turns the seam into something real;
demoable live from Claude Desktop.

**B — leave it, describe it accurately.** Costs nothing. Section 6 is defensible
on its own.

**C — drop the MCP framing** and call it a read-only inbox adapter. Fewer
questions, but loses the credentials argument, which is the one worth keeping.

**Recommendation at the time of writing:** B now, A only if time allows. The
two-minute end-to-end demo is worth more than the transport — it is the
difference between a judge seeing the system work and reading about it.

---

## 8. If option A goes ahead — the checklist

1. `pip install mcp`, add it to `requirements.txt` **as an optional extra**. The
   test suite must keep running without it; CI installs requirements and has no
   API key, and a hard dependency would put a transport library in the path of
   the whole test suite, none of which needs one.
2. New file `mcp/serve.py` — the transport only. Import `InboxServer`, register
   the five existing functions, run over stdio. Do **not** reimplement the tools;
   `TOOLS` already holds the descriptors a `tools/list` response needs.
3. Keep `assert_read_only()` in the startup path so the guard runs when serving,
   not only under test.
4. Merchant id from an environment variable, not an argument. A tool call must
   not be able to select the mailbox — that property is currently enforced by
   `_check()` and must survive the transport.
5. A client in `investigator.py`, behind a flag, defaulting to the in-process
   path. The deterministic three calls stay deterministic; the transport changes
   where they go, not who decides them.
6. Test that the served tool list matches `TOOLS` exactly, so a tool added to one
   and not the other fails the build.
7. Only then consider pointing it at a real mailbox — and that is a **separate**
   decision with its own privacy questions, not part of this task.

### Do not, while doing it

- Do not let the model choose the calls. That is a different change with its own
  evaluation problem, and `investigator.py`'s reasoner hook is already wired and
  unevaluated. See BUILD-LOG.md V2.3.
- Do not add a write tool "for later". The read-only property is asserted by a
  test precisely so this cannot happen quietly.

---

*Companion reading: `BUILD-LOG.md` V2.2 and V2.3 for the triage and inbox scope,
`mcp/inbox_server.py`'s module docstring for the constraints and why they exist,
and README "What is real and what is simulated".*
