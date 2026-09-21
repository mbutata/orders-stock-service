# ADR-0007: The integration surface is an HTTP pull feed over the event log

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-FEED-01, REQ-FEED-02, REQ-FEED-03, REQ-FEED-04, REQ-FEED-05

## Context

Other systems, such as marketing, analytics or warehouse tools, need to consume "order accepted" information.
They are owned by other teams, are deployed on their own schedules, and may be down when an order is accepted.
A small consumer that receives and processes the information, printing it, must accompany the surface.
The event log of [ADR-0003](0003-transactional-outbox.md) already holds every accepted order, in commit order, immutably.

## Decision drivers

- Consumers must not lose events while they are down.
- Consumers must be able to replay.
- The server should not track per-consumer state.
- A consumer should be a few dozen lines against a documented HTTP contract.

## Options considered

### Option 1: HTTP pull feed with an integer cursor (chosen)

`GET /order-events?after=<event_id>&limit=<n>` returns events in commit order with `next_after` and `has_more`.

- Good, because a consumer that is down simply resumes from its cursor; nothing is lost and nothing needs redelivery.
- Good, because the server is stateless per consumer: any number of consumers, no registry, no retry queues.
- Good, because replay is free: start from `after=0`.
- Good, because it is plain HTTP and JSON, testable with `curl`, and described in the OpenAPI contract.
- Bad, because consumers poll, adding latency and load.
- Bad, because consumers own their cursor and deduplication, and the server cannot see their lag.

### Option 2: Webhooks

- Good, because delivery is immediate and consumers need no polling loop.
- Bad, because the server then owns delivery: a subscriber registry, retries with backoff, dead-lettering, request signing, and per-subscriber ordering state.
- Bad, because a slow or failing subscriber becomes this system's operational problem.
- Bad, because the demonstration consumer would have to run an HTTP server.

### Option 3: Server-sent events or WebSockets

- Good, because latency is low.
- Bad, because resuming after a disconnect needs a cursor anyway, so it is the pull feed plus connection management.

### Option 4: Long polling on the pull feed

- Good, because it removes most polling latency.
- Bad, because each waiting request holds resources and needs `LISTEN/NOTIFY` wake-ups.
- It is a compatible later addition to Option 1, not an alternative to it.

### Option 5: Direct read access to the table or a view

- Good, because it needs no code.
- Bad, because consumers couple to the storage schema and need database credentials; it is not an interface other teams can build on.

### Option 6: A broker topic

Rejected in [ADR-0002](0002-postgresql-as-message-substrate.md).

## Decision

Option 1.
Events carry a full snapshot of the order at acceptance, so consumers never need to call back.
Delivery is at-least-once for consumers that save their cursor after processing; the consumer contract is in [04-api.md](../04-api.md#consumer-obligations).
`consume-feed` is the reference consumer: it prints one line per event and persists its cursor atomically after each one.

## Consequences

- The feed keeps working through a stock-processing outage, and a consumer outage costs nothing but lag.
- The stock applier and external consumers read the same log through the same function, so the internal consumer continuously proves the contract.
- Revisit when consumers need sub-second latency or there are many of them: add long polling with `LISTEN/NOTIFY` wake-ups, or stream the log into a broker; the event schema does not change.
