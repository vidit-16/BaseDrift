# Incremental inbox reads — a cursor table

Written to be picked up cold. Not built yet; this is the design and the reasons,
including one security finding that changes what the key has to be.

**Short version:** the system re-reads the whole mailbox on every run and forgets
what it processed when it restarts. The fix is a watermark plus a bounded
seen-set, in a SQLite table attached to the system, keyed on the
**provider-assigned** message id and never on the `Message-ID:` header.

Scope: the CSV inbox (`data/inbox_dev.csv`) stays. Building against the CSV
first is deliberate — the interface is what matters, and a real mailbox then
becomes a swap rather than a rewrite.

---

## 1. What is broken today

    src/webhook.py:191   self._triaged: set = set()
    src/webhook.py:192   self._seen_events: Dict[str, float] = {}

Both in memory. Both forgotten on restart. Two consequences:

- **Re-processing.** `MCP.from_csv()` loads all 7,176 rows every time and
  `Store.ingest_message()` decides freshness from a set that started empty. A
  restart re-ingests everything and creates a second document for messages
  already handled.
- **The webhook has the same bug.** `_seen_events` is the replay guard. A
  restart forgets which Razorpay events were processed, so a redelivery is
  decided twice. COMPLIANCE.md item 2 already lists this; the inbox work fixes
  both with one table.

Neither is a data-loss bug — the safe state is inaction and a duplicate decision
still holds the payout — but duplicate documents and duplicate audit rows make
the trail harder to read, which is the trail's whole purpose.

---

## 2. THE SECURITY FINDING — what the key must be

`POST /messages` currently takes `message_id` from the request body
(`src/webhook.py:590`). Against a real mailbox the obvious thing is to fill that
from the RFC 5322 `Message-ID:` header.

**Do not.** The sender writes that header.

An attacker sends a change request carrying a `Message-ID` that matches one
already processed. Triage returns `DUPLICATE`, the message is dropped, no
document is created, and the payout later decides with **no email evidence at
all**. That is an attacker-controlled path to making their own message
invisible to the control.

So:

| | |
|---|---|
| **Key on** | the provider-assigned id — Gmail `id`, Graph `id`, IMAP UID |
| **Store** | the `Message-ID:` header as metadata, for correlation and debugging |
| **Never** | treat the header as the identity |

For the CSV inbox the generated `message_id` column plays the provider's role,
because the generator is the provider. When a real mailbox arrives, the column
must map to the provider id and the header goes in its own field.

This is worth a test: a message whose header collides with a processed one must
still be processed.

---

## 3. Why a timestamp alone does not work

Three failure modes. The third is the one that matters.

1. **Ties.** Two messages share a second. `>` silently skips one; `>=`
   reprocesses one.
2. **Clock skew** between the mail server and this system.
3. **Late arrivals.** A message delayed in delivery lands *after* the watermark
   has moved past its timestamp, and is then never seen. In a fraud control,
   "silently never processed" is the worst available failure.

---

## 4. The design

    watermark  = latest_received_at − LAG          (LAG = 24h to start)
    seen_ids   = provider ids seen inside that lag window

Each run:

1. fetch everything at or after `watermark`
2. drop anything whose provider id is already claimed
3. process the rest
4. advance the watermark and prune the seen-set to the lag window

The lag is what catches late arrivals: a trailing window is re-examined every
run, and the id set stops that becoming duplicate work. The seen-set stays
bounded because only the lag window is retained, not all history.

**LAG is a tunable with a real trade-off.** Longer is safer against delayed
delivery and costs a larger re-examined window each run. 24h is a starting
point, not a measured value, and it should be recorded as such wherever it ends
up in code.

---

## 5. The table

SQLite. One file, no server, and it gives the thing that actually matters — a
`UNIQUE` constraint, which is what makes the claim atomic.

```sql
CREATE TABLE processed_message (
  provider_id   TEXT PRIMARY KEY,   -- Gmail id / Graph id / IMAP UID / CSV id
  header_msgid  TEXT,               -- stored, NEVER the key. See section 2.
  received_at   REAL NOT NULL,
  claimed_at    REAL NOT NULL,
  document_id   TEXT                -- NULL until processing completed
);

CREATE INDEX processed_message_received ON processed_message(received_at);

CREATE TABLE cursor (
  source        TEXT PRIMARY KEY,   -- "inbox_dev", later a mailbox address
  watermark     REAL NOT NULL,
  token         TEXT,               -- opaque: Gmail historyId, Graph delta
  updated_at    REAL NOT NULL
);

CREATE TABLE processed_event (      -- the webhook's replay guard, same fix
  event_id      TEXT PRIMARY KEY,
  seen_at       REAL NOT NULL
);
```

`token` is unused by the CSV implementation and exists so a real provider's
opaque cursor has somewhere to live without a migration.

---

## 6. Claim, then process — not the other way round

```
1  INSERT provider_id            -- UNIQUE violation means someone has it: skip
2  process the message
3  UPDATE row with document_id
```

- **Process-then-record** loses messages on a crash between the two.
- **Record-then-process** drops them permanently on a crash.
- **Claim-then-process** leaves a claimed row with a NULL `document_id`, which a
  sweeper can find and retry. The failure is visible and recoverable.

That is the same shape COMPLIANCE.md asks for: durably claim the event, then do
the work, and do not mark it complete until the work actually finishes.

---

## 7. The interface change

One method on the inbox, alongside the existing read-only tools:

```python
def fetch_since(self, cursor, limit=500) -> tuple[list[StoredMessage], Cursor]:
    ...
```

- The CSV implementation filters on `received_at` and returns a new watermark.
- A Gmail implementation passes `historyId` and returns the next one.
- **The caller never knows which.** Same argument as `search_history(before=)`,
  which already reads the mailbox at a point in time rather than as it is now.

`Store._triaged` and `Store._seen_events` both become SQLite-backed. The public
behaviour of `ingest_message()` does not change; only where it remembers.

---

## 8. Checklist

1. `src/inbox_state.py` — SQLite, schema above, opened once. Path from an
   environment variable with a sane default; `:memory:` for tests so the suite
   stays hermetic.
2. `claim(provider_id, header_msgid, received_at) -> bool` — the atomic claim.
3. `complete(provider_id, document_id)`.
4. `get_cursor(source)` / `advance_cursor(source, watermark, token)`.
5. `fetch_since()` on `InboxServer`, CSV-backed.
6. Rewire `Store.ingest_message()` to claim first, and `Store.seen_before()` to
   use `processed_event`.
7. Tests:
   - a restart does not re-ingest — the point of the whole exercise
   - a colliding `Message-ID:` header is still processed (section 2)
   - a late-arriving message inside the lag window is picked up
   - a claimed-but-incomplete row is retried, not skipped
   - the suite still runs with no file on disk

### Do not

- Do not key on the `Message-ID:` header. Section 2 is the reason and it is not
  a theoretical one.
- Do not drop the lag window to zero because it looks redundant. It is the only
  thing catching late delivery.
- Do not put this behind the same in-memory `Store` lifetime. If it does not
  survive a restart it has not fixed anything.

---

*Companion reading: `notes/mcp.md` for why the inbox is behind a tool boundary at
all, `COMPLIANCE.md` items 2 and 3 for the durability and access-control gaps
this partly closes, and `mcp/inbox_server.py` for the existing read-only tools.*
