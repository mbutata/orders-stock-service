# ADR-0002: PostgreSQL is the message substrate; no external broker

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-OUT-01, REQ-OUT-02, REQ-FEED-01, REQ-OPS-02

## Context

Accepted orders must reach two consumers: the stock applier, and other systems through the integration surface.
Work must survive a stock-processing outage of any length and any process restart.
PostgreSQL is mandatory and already the system of record; the system must run natively without containers.
The simplest viable mechanisms, polling or an outbox, are explicitly acceptable.

## Decision drivers

- Atomicity between an accepted order and the work it creates.
- Durability across outages and restarts.
- Replay for external consumers.
- Local operability: nothing to install beyond Python and PostgreSQL.
- Proportionality.

## Options considered

### Option 1: PostgreSQL tables as the log, read by polling (chosen)

- Good, because the event is written in the same transaction as the order, so atomicity is free.
- Good, because the log is durable, replayable and inspectable with plain SQL.
- Good, because there is no new infrastructure to install, configure, secure or monitor.
- Bad, because polling adds latency, bounded by the poll interval, and one indexed query per interval per consumer.
- Bad, because throughput is bounded by a single PostgreSQL node and the append lock of [ADR-0004](0004-commit-ordered-event-log.md).

### Option 2: RabbitMQ

- Good, because it provides push delivery, acknowledgements and dead-lettering.
- Bad, because publishing after the commit is a dual write, so an outbox and a relay are still needed for atomicity: strictly more moving parts.
- Bad, because queues are consumed destructively, so replay for external consumers needs a separate design.
- Bad, because it is another server to run locally.

### Option 3: Kafka or Redpanda

- Good, because a partitioned, replayable log is exactly the right model at scale.
- Bad, because it still needs an outbox and a relay, or change data capture, for atomicity.
- Bad, because its local footprint and operational weight are far out of proportion.

### Option 4: Redis Streams

- Good, because it is light and supports consumer groups.
- Bad, because it adds a second datastore with its own persistence settings, and publishing is still a dual write.

### Option 5: `LISTEN/NOTIFY` as the transport

- Good, because it is built in and has low latency.
- Bad, because notifications are not durable: a listener that is down misses them, which is exactly the outage case.
- Bad, because payloads are limited to 8000 bytes and there is no replay.

### Option 6: A PostgreSQL queue extension or library (pgmq, procrastinate)

- Good, because it packages queue semantics.
- Bad, because pgmq is an extension that stock PostgreSQL installations do not ship.
- Bad, because a job queue models tasks to be consumed once, not a replayable log that other systems read at their own pace.
- Bad, because it adds a dependency to do what one table and a cursor already do.

## Decision

Option 1: the `order_events` table is the log, and every consumer polls it by cursor.
`LISTEN/NOTIFY` is not used; it could later wake consumers early, but polling stays the correctness path.

## Consequences

- One datastore holds business data and messages, so they can never disagree.
- An outage of any length loses nothing; the backlog is rows in a table.
- Latency is at least the poll interval: 0.5 seconds for the stock applier by default.
- Revisit when many teams need push delivery or intake outgrows one node: stream the same table into a broker with change data capture, which changes nothing in intake.
