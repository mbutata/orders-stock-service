# ADR-0003: Downstream work is recorded with a transactional outbox

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-ORD-06, REQ-ORD-07, REQ-FEED-02, REQ-OUT-01, REQ-OUT-03

## Context

Accepting an order creates two pieces of downstream work: decrementing stock, and telling other systems.
Neither may be lost, neither may be done for an order that does not exist, and neither may be done twice.
Intake must succeed while stock processing is unavailable.
Any process can crash between any two statements.

## Decision drivers

- No lost work and no phantom work under crashes.
- Intake independent of stock processing.
- One contract for internal and external consumers where possible.
- Consumers read published facts, not another component's internal tables.

## Options considered

### Option 1: Decrement stock synchronously in the acceptance transaction

- Good, because stock is always current.
- Bad, because intake then depends on stock processing, which violates REQ-OUT-01.
- Bad, because hot SKUs become lock contention in the request path.
- Bad, because other systems would still need a separate notification mechanism.

### Option 2: Commit the order, then call the stock component or publish a message

- Good, because it is easy to write.
- Bad, because a crash between the commit and the call loses the work, and calling first creates work for an order that may never commit.
- Bad, because making it reliable needs retry state, which is an outbox by another name.

### Option 3: Transactional outbox (chosen)

Append an `order.accepted` event to `order_events` in the same transaction as the order; consumers read the log.

- Good, because the order and its event commit atomically, so work is neither lost nor invented.
- Good, because intake never waits for a consumer.
- Good, because the stock applier and external consumers read the same log and the same payload.
- Bad, because a log with concurrent writers must be made commit-ordered ([ADR-0004](0004-commit-ordered-event-log.md)).
- Bad, because the log grows and consumers must be idempotent.

### Option 4: The order status as the work queue

The stock applier claims `orders WHERE status = 'accepted'` with `FOR UPDATE SKIP LOCKED`, with no event table.

- Good, because no extra table is needed and there is no ordering problem.
- Bad, because external consumers still need a feed, so a log is needed anyway.
- Bad, because Inventory would read Orders' internal tables rather than a published contract.
- Bad, because without a global position there is no `as_of_event_id` to tell readers how fresh stock is.

### Option 5: Change data capture from the orders tables

Logical decoding, for example with Debezium or wal2json.

- Good, because the application writes nothing extra.
- Bad, because it needs replication slots, a connector process and slot monitoring.
- Bad, because consumers would couple to table layout instead of an event schema.

## Decision

Option 3.
The event payload is a full snapshot of the order at acceptance, so no consumer needs to call back for details.
The stock applier reads the log through Orders' `read_events`, the same function behind the public feed.

## Consequences

- An accepted order always has exactly one event (INV-EVT-1), enforced by the transaction and backed by a unique constraint.
- Stock processing can be down indefinitely without affecting intake; its backlog is the unread part of the log.
- Every consumer must tolerate reading an event more than once; the stock applier does so with a guarded status transition, external consumers by `event_id`.
- Revisit when a second event type is needed: it is appended to the same log and preserves per-order ordering.
