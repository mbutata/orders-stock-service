# ADR-0004: Event IDs follow commit order, enforced by an advisory lock

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-FEED-03, REQ-OUT-02, REQ-STK-02

## Context

Consumers of the event log, the stock applier and external systems, track progress as the highest `event_id` they have processed.
Identity values are assigned when an insert executes, but transactions commit in their own order.
If T1 draws `event_id` 10 and T2 draws 11, and T2 commits first, a consumer can read 11 and move its cursor past 10 before T1 commits.
Event 10 then becomes visible behind every cursor and is never read: an order whose stock is never decremented and which no other system ever sees.
This happens routinely under concurrent intake.

## Decision drivers

- No consumer may ever skip a committed event.
- A cursor must stay a single integer that external consumers can store and compare.
- The mechanism must be easy to reason about and to test.
- Intake throughput must remain far above what this system needs.

## Options considered

### Option 1: A transaction-scoped advisory lock around the append (chosen)

Each acceptance calls `pg_advisory_xact_lock(8030591472495849076)` immediately before inserting its event, as its last statement; the lock is released when the transaction ends.

- Good, because the next appender cannot draw an ID until the previous appender's commit is visible, so ID order equals commit order.
- Good, because PostgreSQL releases the lock automatically on commit, rollback or client death.
- Good, because it writes nothing and blocks no reader.
- Bad, because appends are serialized: intake is capped near the WAL flush rate, around a thousand orders per second on local SSD storage.
- Bad, because rolled-back appends leave gaps that consumers must tolerate.

### Option 2: `LOCK TABLE order_events IN SHARE ROW EXCLUSIVE MODE`

- Good, because it has the same ordering effect.
- Bad, because it is a heavier lock that also conflicts with autovacuum's lock on the table, and its purpose is less obvious to a reader.

### Option 3: A counter row

`UPDATE log_head SET last_event_id = last_event_id + 1 RETURNING last_event_id` supplies IDs.

- Good, because IDs are gapless, since a rollback also rolls back the counter.
- Bad, because the same serialization now costs a new row version per order on one hot row.

### Option 4: A snapshot visibility horizon

Store `pg_current_xact_id()` with each event and return only events whose transaction ID is below `pg_snapshot_xmin(pg_current_snapshot())`, with a cursor of transaction ID and event ID.

- Good, because writers are not serialized.
- Bad, because the cursor becomes a pair that consumers must handle.
- Bad, because readers stall behind any long-running transaction in the database, including an idle `psql` session.
- Bad, because the argument is subtle and hard to test convincingly.

### Option 5: A visibility delay

Return only events older than a few seconds.

- Good, because it is trivial to implement.
- Bad, because it is a heuristic: a transaction that stalls longer than the delay still loses its event.

### Option 6: Per-consumer processed sets instead of cursors

- Good, because order no longer matters.
- Bad, because it needs server-side state per consumer per event, which an HTTP feed for unknown external consumers cannot keep.

## Decision

Option 1.
The lock key is a constant, `0x6F72646576656E74` (the ASCII bytes of `ordevent`), defined once in the Orders component.

## Consequences

- INV-EVT-3 holds, and AC-FEED-04 demonstrates it with two concurrent acceptances.
- Consumers can use a plain integer cursor and never skip an event; they must not assume contiguity.
- Everything before the append, pricing and the order and item inserts, stays concurrent; only the last statement and the commit are serialized.
- Revisit when intake approaches the append ceiling: move to Option 4, which changes the cursor format but not the event schema.
