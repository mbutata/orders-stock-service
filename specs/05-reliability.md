# 05 - Reliability and consistency

This document specifies how the system stays correct under duplicate submissions, concurrent requests, an unavailable stock applier, and processes that die mid-flight.
Every claim names the PostgreSQL mechanism it relies on and why that mechanism was chosen over the alternatives.
The concurrency behaviour described here was checked against PostgreSQL 14 with a prototype before it was written down; the acceptance scenarios in [06-acceptance.md](06-acceptance.md) turn each claim into a test.

## Guarantees at a glance

| Guarantee | Mechanism | Requirements | Verified by |
| --- | --- | --- | --- |
| At most one order per `order_ref`, under any concurrency | Primary key on `orders.order_ref`, `INSERT ... ON CONFLICT DO NOTHING`, `READ COMMITTED` | REQ-DUP-01 to REQ-DUP-04 | AC-DUP-01 to AC-DUP-08 |
| An order and its event exist together or not at all | One transaction for order, items and event (transactional outbox) | REQ-ORD-07, REQ-FEED-02 | AC-ORD-07, AC-FEED-03 |
| Intake never depends on stock processing | The acceptance transaction touches no Inventory table | REQ-ORD-06, REQ-OUT-01 | AC-ORD-08, AC-OUT-01 |
| `event_id` order is commit order | `pg_advisory_xact_lock` held from the event insert to commit | REQ-FEED-03 | AC-FEED-04 |
| Each order's stock is applied exactly once | Offset, guarded status transition and stock update in one transaction | REQ-STK-02, REQ-STK-03, REQ-OUT-03 | AC-STK-01, AC-OUT-04, AC-OUT-05 |
| One active stock applier at a time | `SELECT ... FOR UPDATE` on the offset row | REQ-OUT-04 | AC-OUT-06 |
| Stock converges after an outage | Replay from the offset; decrements commute | REQ-OUT-02 | AC-OUT-02, AC-OUT-03 |
| The feed is replayable, totally ordered, at-least-once | Cursor over an append-only, commit-ordered log | REQ-FEED-01 to REQ-FEED-04 | AC-FEED-01 to AC-FEED-05 |
| `on_hand` and `as_of_event_id` describe the same moment | Both read in one statement | REQ-STK-05 | AC-STK-04 |

## Transaction model

Every connection uses `READ COMMITTED`, set explicitly rather than inherited from the server's `default_transaction_isolation`.

- The duplicate-resolution flow needs a statement to see a row that a concurrent transaction committed after this transaction began.
  Under `READ COMMITTED` each statement takes a fresh snapshot, so the `SELECT` after a conflicting insert sees the winner's order.
  Under `REPEATABLE READ` that `SELECT` could not see it, and `INSERT ... ON CONFLICT DO NOTHING` against a row committed after the snapshot fails with `serialization_failure` (SQLSTATE `40001`); both behaviours were confirmed on PostgreSQL 14.
- No transaction here needs a stable multi-statement snapshot.
  Correctness comes from the unique index, row locks and the advisory lock, not from snapshot isolation.
- `SERIALIZABLE` would also be correct, but it reports conflicts as `40001` errors that every caller must retry, and no invariant here needs it.

The one read that must be internally consistent, the stock level with its offset, is a single statement, so it sees a single snapshot.

`statement_timeout = 5s` bounds every wait on a lock: the unique-index wait of a concurrent duplicate, the append lock, and the offset row lock.
A pathological wait therefore surfaces as `503` or a worker retry instead of a hang.

The system has three write transactions: acceptance, stock application, and seeding.

### The acceptance transaction

`accept_order` runs these statements in one transaction.
Placeholders are bound parameters.

```sql
BEGIN;  -- READ COMMITTED

-- 1. Retry fast path: is this order_ref already taken?
SELECT customer_id, status, total_cents, accepted_at, stock_committed_at
  FROM orders WHERE order_ref = $order_ref;
--    Found: load its items, compare content, COMMIT, respond 200 or 409.

-- 2. Price the order from the catalogue.
SELECT sku, price_cents FROM products WHERE sku = ANY($skus);
--    Any SKU missing: ROLLBACK, respond 422 unknown_sku.

-- 3. Claim the order_ref.
INSERT INTO orders (order_ref, customer_id, total_cents)
VALUES ($order_ref, $customer_id, $total_cents)
ON CONFLICT (order_ref) DO NOTHING
RETURNING accepted_at;
--    No row returned: a concurrent transaction committed this order_ref first.
--    Load the order and its items (this statement's snapshot sees the winner),
--    compare content, COMMIT, respond 200 or 409.

-- 4. Items, in ascending SKU order.
INSERT INTO order_items (order_ref, sku, qty, unit_price_cents) VALUES (...), (...);

-- 5. Serialize appends to the event log.
SELECT pg_advisory_xact_lock(8030591472495849076);

-- 6. Append the event. Its payload is built from the values inserted above.
INSERT INTO order_events (event_type, event_version, order_ref, occurred_at, payload)
VALUES ('order.accepted', 1, $order_ref, $accepted_at, $payload)
RETURNING event_id;

COMMIT;  -- releases the advisory lock
```

`total_cents` is computed in Python as the sum of `qty * price_cents` over the items before step 3.
The payload's `accepted_at` and the event's `occurred_at` both take the `accepted_at` returned by step 3, so the three timestamps are equal by construction.

## Duplicate submissions

### Definition

A submission is a duplicate when an order with its `order_ref` already exists, or is being accepted by a concurrent transaction.
Content is the same when `customer_id` is equal and the items form the same set of `(sku, qty)` pairs.
Prices are not part of the comparison: the client does not send them, and the stored prices are the ones that count.

### What a caller observes

| Situation | First submission | Later submission |
| --- | --- | --- |
| Identical resubmission, after the first completed | `201` | `200` with the order in its current state |
| Different content, after the first completed | `201` | `409 order_ref_conflict`; stored order unchanged |
| Identical submissions in flight at the same time | Exactly one `201` | Each waits briefly on the unique index, then `200` |
| Different contents in flight at the same time | Exactly one `201`; commit order decides which content wins | `200` if identical to the winner, otherwise `409` |
| First attempt committed, but its response was lost | Client saw a timeout or reset | The retry gets `200` with the order |
| First attempt did not commit (crash, `503`) | Client saw an error | The retry gets `201` |
| Retry after the catalogue price changed | `201` at the old price | `200` at the old price; never repriced |
| Resubmission of an existing `order_ref` with an item whose SKU is unknown | `201` | `409`: step 1 answers before the catalogue is consulted |

### Why the race is safe

Consider two transactions, T1 and T2, submitting the same `order_ref` concurrently.
Both may pass step 1, because neither order is committed yet.
Both reach step 3.
Whichever inserts first, say T1, creates the index entry; T2's insert finds T1's uncommitted entry and waits for T1 to finish.
If T1 commits, T2's `ON CONFLICT DO NOTHING` returns no row, and T2's next statement, under `READ COMMITTED`, sees T1's committed order and compares against it.
If T1 rolls back, T2's insert proceeds and T2 becomes the winner.
In every interleaving exactly one transaction inserts the order, and every other transaction either observes it or inserted it itself.
The step-1 read is an optimisation for the common retry case; it is not the guard, the unique index is.

### Why these mechanisms and not others

- **A unique index** is the only arbiter of uniqueness that is atomic across transactions and processes.
  Any application-level check is check-then-act: two transactions can both see "no row".
- **`ON CONFLICT DO NOTHING` rather than catching `unique_violation`.**
  A failed plain `INSERT` aborts the transaction; reading the winner would then need a savepoint or a second transaction, and every duplicate would log an error on the server.
  `DO NOTHING` keeps the transaction usable.
- **`DO NOTHING` plus a `SELECT` rather than `ON CONFLICT DO UPDATE ... RETURNING`.**
  The no-op-update idiom returns the existing row in one statement, but it writes a new row version and takes a row lock on the existing order for every duplicate, producing dead tuples and contending with the stock applier's status update.
  Duplicates should read, not write.
- **Not a per-key advisory lock** such as `pg_advisory_xact_lock(hashtext(order_ref))`.
  It works, but duplicates what the index already guarantees, and hash collisions would serialize unrelated orders.
- **Not `SERIALIZABLE`**, for the reasons in [Transaction model](#transaction-model).
- **Not a separate idempotency-key store.**
  A generic key table with stored responses needs an expiry policy and response caching; here the key is the order's own identity, so the order row is the record.
  See [ADR-0005](adr/0005-order-ref-idempotency.md).

## Atomic acceptance and downstream work

The order row, its items and its `order.accepted` event commit in one transaction.
There is no moment at which an order exists without its event, or an event exists without its order.
Both downstream consumers, the stock applier and the public feed, derive their work from that event alone.
Nothing else is written, and no network call is made inside or after the transaction to hand work on.
A crash at any point therefore cannot lose downstream work, and cannot create work for an order that does not exist.
This is the transactional outbox; the rejected alternatives, such as committing the order and then calling a broker or the stock component, are in [ADR-0003](adr/0003-transactional-outbox.md).

## Commit-ordered event log

### The problem

Identity values are drawn when an `INSERT` executes, but transactions commit in their own order.
Suppose T1 draws `event_id` 10 and T2 draws 11, T2 commits, a consumer reads 11 and saves its cursor at 11, and then T1 commits.
Event 10 is now visible, but it is behind every cursor and will never be read.
This is not an exotic case; it happens routinely under concurrent intake, and it silently loses orders for every cursor-based consumer, including the stock applier.

### The mechanism

Each acceptance takes `pg_advisory_xact_lock(8030591472495849076)` immediately before inserting its event, which is its last statement.
A transaction-scoped advisory lock is released only when the transaction ends, and PostgreSQL releases a committing transaction's locks only after the commit is recorded and visible to other sessions.
So the next appender cannot draw an `event_id` until the previous appender's event is visible or rolled back.
Therefore `event_id` order equals commit order, and INV-EVT-3 holds: when a reader sees event `N`, every lower event that will ever exist is already visible.
A rolled-back appender leaves a gap that is never filled, which is why consumers must not assume contiguous IDs.

The key `8030591472495849076` is `0x6F72646576656E74`, the ASCII bytes of `ordevent`, defined once in the Orders component.
The database is dedicated to this system and nothing else takes advisory locks, so the key cannot collide.

### Why an advisory lock

- It is an application-defined mutex whose lifetime PostgreSQL ties to the transaction, including release when the client dies.
- It touches no table, so it writes no row versions and blocks no reader.
- `LOCK TABLE order_events IN SHARE ROW EXCLUSIVE MODE` has the same effect, but it is a heavier table lock whose intent is less obvious to a reader.
- A counter row updated with `UPDATE ... RETURNING` would give gapless IDs, but it creates a hot row with a new tuple version per order.
- Filtering reads by the transaction visibility horizon, `pg_snapshot_xmin(pg_current_snapshot())` against an `xid8` column, avoids serializing writers, but needs a two-part cursor, stalls behind any long-running transaction, and is much harder to reason about.
- Reading only events older than some delay is a heuristic that fails exactly when a transaction stalls.

[ADR-0004](adr/0004-commit-ordered-event-log.md) records this decision.

### Cost

The lock is held from the event insert until the end of the commit: one index insert and one WAL flush.
Appends are therefore serialized at roughly the WAL flush rate, on the order of a thousand per second on local SSD storage.
Pricing and the order and item inserts, which come before the lock, stay fully concurrent.

## Stock application

### The stock application transaction

`apply_next_batch` runs these statements in one transaction.

```sql
BEGIN;  -- READ COMMITTED

-- 1. Become the only active applier and read the offset.
SELECT last_event_id FROM consumer_offsets
 WHERE consumer = 'stock-applier' FOR UPDATE;

-- 2. Read the next batch (Orders.read_events).
SELECT event_id, event_type, event_version, order_ref, occurred_at, payload
  FROM order_events
 WHERE event_id > $offset
 ORDER BY event_id
 LIMIT $batch_size;
--    No rows: COMMIT; the worker waits one poll interval.

-- 3. Transition the batch's orders (Orders.mark_stock_committed).
UPDATE orders
   SET status = 'stock_committed', stock_committed_at = now()
 WHERE order_ref = ANY($order_refs) AND status = 'accepted'
RETURNING order_ref;

-- 4. For the orders returned by step 3 only, sum qty per SKU from the event payloads,
--    then, per SKU in ascending order:
UPDATE stock_levels
   SET on_hand = on_hand - $qty, updated_at = now()
 WHERE sku = $sku
RETURNING on_hand;
--    Each statement must update exactly one row; otherwise raise StockInvariantViolation.
--    A negative on_hand is logged as a backorder.

-- 5. Advance the offset to the last event_id of the batch.
UPDATE consumer_offsets
   SET last_event_id = $last_event_id, updated_at = now()
 WHERE consumer = 'stock-applier';

COMMIT;
```

The applier skips events whose `event_type` it does not handle and still advances past them.
It raises `StockInvariantViolation` for an `order.accepted` event with an `event_version` above 1.
Events whose order was not returned by step 3 are counted as `orders_skipped` and logged as a warning; this is only reachable if the offset has been moved back by hand.

### Why each order is applied exactly once

1. **Reading is at-least-once.**
   An event is read again until an offset beyond it is committed.
2. **Effect and progress are atomic.**
   The status transitions, the stock decrements and the offset advance commit together, so there is no state in which stock changed but progress was not recorded, or the reverse.
3. **The status transition is an idempotency guard independent of the offset.**
   An order already `stock_committed` is excluded by `WHERE status = 'accepted'`, and its items contribute nothing to step 4.
   Replaying the whole log from offset 0 changes nothing.

At-least-once reading combined with an atomic, idempotent effect gives exactly-once effect.
In the standard vocabulary: the log delivers events to the stock applier at least once, and stock is changed exactly once per accepted order.
This holds only because the event log, the order status, the stock levels and the offset live in one database and change in one local transaction; no distributed transaction or deduplication store is involved.

### One active applier

`SELECT ... FOR UPDATE` on the offset row admits one applier transaction at a time.
A second applier, from an overlapping restart or an operator mistake, blocks at step 1 until the first commits, then reads the advanced offset and continues from there.
Two appliers never hold stock locks at the same time, so they cannot deadlock, and the guard in step 3 would neutralise a double read even if they could.

- **Not `SKIP LOCKED`.**
  `SKIP LOCKED` suits competing consumers claiming independent work items.
  Here progress is a single offset; competing claims would need per-event claim state and would give up ordered application.
  A second applier waiting is exactly the wanted behaviour.
- **Not a session-level advisory lock for leader election.**
  It would work, but the row lock sits on the very row that records progress, needs no extra key, and is released with the transaction.
- **Not "orders with status `accepted`" as the work queue.**
  Polling `orders WHERE status = 'accepted'` with `SKIP LOCKED` needs no offset, but it reads Orders' internal tables instead of the published event, gives up ordered application, and leaves no `as_of_event_id` to tell a reader how fresh stock is.
  See [ADR-0003](adr/0003-transactional-outbox.md).

### Lock ordering

Within a batch, order rows are locked before stock rows, and stock rows in ascending SKU order.
Intake only inserts order rows and never updates them, and `seed` only inserts missing stock rows.
No other transaction locks these rows in a conflicting order, so no lock cycle exists.

### Outage and catch-up

While the stock applier is not running:

- Intake is unaffected.
  The acceptance transaction touches no Inventory table and waits for nothing the applier holds.
  AC-ORD-08 proves this by holding `ACCESS EXCLUSIVE` locks on both Inventory tables while an order is accepted.
- Events accumulate in the log.
  Nothing is buffered in memory, so an outage of any length loses nothing.
- `GET /stock/{sku}` returns the last applied state, with `as_of_event_id` visibly behind the head of the log.
- `GET /order-events` keeps serving new events; the feed does not depend on the applier.

When the stock applier starts again:

- It logs its offset, the head of the log and the lag.
- It applies batches back to back, without sleeping, until it reaches the head.
- Catch-up is the normal loop with a backlog.
  It needs no operator action and no special mode.

Stock converges to the no-outage value because decrements commute.
The final `on_hand` of a SKU depends only on the set of applied orders, not on when or in which batches they were applied, so after catch-up every SKU satisfies INV-STK-2 over the same orders as it would have without the outage (REQ-OUT-02).
Allowing negative stock is what makes this true: if insufficient stock rejected orders, the result would depend on processing order and timing.

When idle, the applier picks up a new order within one poll interval, 0.5 seconds by default.
A backlog of `N` events drains in `ceil(N / 100)` transactions at the default batch size.

### Insufficient stock

The applier decrements unconditionally; `on_hand` may become negative, and the order still becomes `stock_committed`.

- Acceptance cannot depend on stock (REQ-OUT-01), so the system can always sell more than it holds, whatever policy follows.
- Rejecting an order after it was acknowledged with `201` breaks a promise already made to the customer, and needs a compensation flow and a third failure path.
- Negative stock keeps conservation (INV-STK-2) exact and catch-up independent of order.

The oversell is visible: `GET /stock/{sku}` returns the negative number, and the applier logs `backorder: <sku> on_hand=<n>` at warning level whenever a batch leaves a SKU below zero.
[ADR-0006](adr/0006-asynchronous-stock-decrement.md) records the alternatives.

### Poison events

An event can fail to apply only if an invariant was broken outside the application: an event names a SKU without a stock level (INV-STK-1), or carries an `event_version` the applier does not understand.
The applier then rolls back, logs an error naming the event, and retries with backoff without advancing.
It never skips the event, because skipping would silently lose a stock decrement.
Visibly stale stock, with `as_of_event_id` frozen, is preferable to silently wrong stock.
Recovery is an operator's data fix, after which the applier continues by itself.

## Integration surface

The guarantees offered to consumers of `GET /order-events`:

- **Delivery.**
  At-least-once for a consumer that saves its cursor after processing.
  Its obligation is idempotent processing keyed by `event_id`.
  The full consumer contract is in [04-api.md](04-api.md#consumer-obligations).
- **Ordering.**
  One total order by `event_id`, equal to commit order.
  There is no ordering relative to stock application: a consumer may see an event before or after the stock applier has applied it.
- **No duplicates in the log.**
  Duplicate submissions never append; a consumer sees an event twice only by re-reading it.
- **Replay.**
  Any consumer can re-read from `after=0` at any time; the append-only trigger guarantees it reads the same events.
- **Independence.**
  The feed is served by `api` and is unaffected by the stock applier.

## Read consistency

| Read | Consistency |
| --- | --- |
| `GET /orders/{order_ref}` | Read-your-writes: it reads the same primary that committed the order. |
| `GET /stock/{sku}` | One statement, so `on_hand` and `as_of_event_id` agree. Eventually consistent with intake; staleness is explicit. |
| `GET /order-events` | One snapshot per request; commit ordering means pagination never skips an event. |

## Crashes and restarts

No process holds state in memory that correctness depends on (REQ-OPS-01).

| Failure point | State afterwards | Recovery | Visible to the client |
| --- | --- | --- | --- |
| `api` dies before an acceptance commits | PostgreSQL rolls the transaction back when the connection drops: no order, no event | None | Connection error; a retry gets `201` |
| `api` dies after the commit, before responding | Order and event committed | None | Connection error; a retry gets `200` |
| Connection lost during `COMMIT` | Committed or not; the client cannot tell | A retry resolves it | `503` or connection error; a retry gets `200` or `201` |
| `stock-worker` killed mid-batch (`SIGKILL`) | Batch rolled back: no stock, status or offset change | Restart re-reads from the unchanged offset | Stock stays stale until restart |
| `stock-worker` dies after a commit | Batch fully applied | Restart continues after the new offset | None |
| Two `stock-worker` processes overlap | The second waits on the offset row lock | It continues from the advanced offset | None |
| `consume-feed` dies after printing, before saving its cursor | Cursor still before the event | Restart re-reads and re-prints the event | One duplicate line, identified by `event_id` |
| `consume-feed` dies after saving its cursor | Cursor at the event | Restart continues | None |
| PostgreSQL restarts | Committed data is durable; in-flight transactions abort | The pool replaces broken connections; the worker backs off and retries | `503` during the outage; retries succeed afterwards |
| `seed` interrupted | One transaction: all or nothing | Run it again | None |

## Network and dependency failures

| Link | Behaviour on failure |
| --- | --- |
| Client to `api` | The client retries with the same `order_ref`; `burst` does so up to 5 times with backoff from 0.2 seconds. |
| `api` to PostgreSQL | `503 service_unavailable` with `Retry-After: 1`; `api` keeps running and the pool reconnects. |
| `stock-worker` to PostgreSQL | Exponential backoff from 0.5 to 10 seconds; it resumes from its offset. |
| `consume-feed` to `api` | Exponential backoff from 0.5 to 10 seconds; it resumes from its cursor. |
| PostgreSQL down | Nothing is accepted; this is a non-goal to tolerate (NG-12). |

## Known limits

- Event appends are serialized, capping intake at roughly the WAL flush rate ([ADR-0004](adr/0004-commit-ordered-event-log.md) names the path past it).
- The event log grows without bound (NG-09).
- One stock applier processes batches of 100 events; its throughput exceeds the append ceiling, so it is not the bottleneck (NG-11).
- Stock latency is bounded below by the 0.5-second poll interval; `LISTEN/NOTIFY` could remove it, but polling would remain the correctness path.
