# 01 - Requirements

This document states what the system must do, as individually testable requirements.
Every other specification, every acceptance scenario in [06-acceptance.md](06-acceptance.md), and every test traces back to an ID defined here.

The key words MUST, MUST NOT, SHOULD and MAY are used as described in RFC 2119 and RFC 8174.

## Scope

The system accepts orders over HTTP, stores them in PostgreSQL, reduces stock for every accepted order, and publishes an "order accepted" feed that other systems can consume.
It is deliberately small.
Two failure modes are designed for and demonstrated: duplicate order submissions, and a temporary outage of the stock-processing component followed by catch-up.

## ID scheme

| Prefix | Meaning |
| --- | --- |
| `REQ-CAT-nn` | Catalogue (products and prices) |
| `REQ-ORD-nn` | Order intake |
| `REQ-STK-nn` | Stock levels and stock application |
| `REQ-FEED-nn` | Integration surface: the order-accepted feed |
| `REQ-API-nn` | HTTP contract as a whole |
| `REQ-OPS-nn` | Running, seeding and demonstrating the system |
| `REQ-ARCH-nn` | Structural rules for the code |
| `REQ-DUP-nn` | Unhappy path 1: duplicate submissions |
| `REQ-OUT-nn` | Unhappy path 2: stock-processing outage and catch-up |
| `NG-nn` | Non-goals: things the system deliberately does not do |

IDs are stable.
A requirement that is dropped keeps its ID and is marked withdrawn; IDs are never reused.

Each requirement names how it is verified: by an acceptance scenario (`AC-...`), by a demo step (`D-nn` in [07-demo.md](07-demo.md)), or by inspection.

## Functional requirements

### Catalogue

**REQ-CAT-01 - Products are persisted reference data.**
Each product has a `sku`, a `name` and a `price_cents`, and is stored in PostgreSQL.
The catalogue is the only source of prices used to price orders.
Verified by: AC-ORD-01, AC-OPS-02.

### Order intake

**REQ-ORD-01 - Create an order.**
`POST /orders` with an `order_ref`, a `customer_id` and one or more `items` (`sku`, `qty`) MUST persist the order in PostgreSQL with status `accepted` and respond `201 Created` with the order representation and a `Location` header.
Verified by: AC-ORD-01, AC-ORD-09.

**REQ-ORD-02 - Price at the time of the order.**
At acceptance, each item MUST record the product's current `price_cents` as its `unit_price_cents`.
The line total is `qty * unit_price_cents`, and the order total is the sum of the line totals.
Later catalogue price changes MUST NOT change an accepted order.
Verified by: AC-ORD-01, AC-ORD-04, AC-STK-05.

**REQ-ORD-03 - Fetch an order.**
`GET /orders/{order_ref}` MUST return the order's details and current status, or `404` with problem code `order_not_found` when no such order exists.
Verified by: AC-ORD-02, AC-ORD-03, AC-ORD-09.

**REQ-ORD-04 - Reject malformed requests.**
A create request that violates the request schema in [04-api.md](04-api.md) MUST be rejected with `422` and problem code `validation_error`, and MUST have no effect.
Verified by: AC-ORD-05.

**REQ-ORD-05 - Reject unknown products.**
A create request for a new `order_ref` that references a `sku` absent from the catalogue MUST be rejected with `422` and problem code `unknown_sku`, and MUST have no effect.
Verified by: AC-ORD-06.

**REQ-ORD-06 - Intake is independent of stock processing.**
Accepting an order MUST NOT read, write or lock any Inventory table, and MUST NOT wait for the stock applier.
Verified by: AC-ORD-08.

**REQ-ORD-07 - The order and its event are recorded atomically.**
Persisting an accepted order and appending its `order.accepted` event MUST happen in one database transaction: after any failure, either both exist or neither does.
Verified by: AC-ORD-01, AC-ORD-07.

### Stock

**REQ-STK-01 - Stock levels are persisted.**
Each product has exactly one stock level, `on_hand`, stored in PostgreSQL.
Verified by: AC-OPS-02, AC-STK-04.

**REQ-STK-02 - Each accepted order reduces stock exactly once.**
For every accepted order, the `on_hand` of each item's SKU MUST be reduced by the item's `qty` exactly once.
Verified by: AC-STK-01, AC-STK-02, AC-STK-05, AC-DUP-04.

**REQ-STK-03 - The status change and the stock change are one step.**
An order MUST move from `accepted` to `stock_committed` in the same transaction that applies its stock decrements.
Verified by: AC-STK-01, AC-OUT-04.

**REQ-STK-04 - Insufficient stock does not reject an accepted order.**
A decrement MUST be applied even when it makes `on_hand` negative.
Negative `on_hand` is a backorder: units sold but not physically available.
The order still reaches `stock_committed`.
Verified by: AC-STK-03.

**REQ-STK-05 - Read current stock for a SKU.**
`GET /stock/{sku}` MUST return the SKU's `on_hand` and `as_of_event_id`, the highest event ID whose effect is included in `on_hand`, or `404` with problem code `sku_not_found`.
Verified by: AC-STK-04, AC-OUT-01, AC-OUT-02.

### Integration surface

**REQ-FEED-01 - A pull feed of accepted orders.**
`GET /order-events` MUST return `order.accepted` events in ascending `event_id` order, paginated by an `after` cursor and a `limit`.
Verified by: AC-FEED-01, AC-FEED-02, AC-FEED-06, AC-ORD-09.

**REQ-FEED-02 - One event per accepted order.**
Each accepted order MUST have exactly one `order.accepted` event.
Duplicate, conflicting and rejected submissions MUST NOT append events.
Verified by: AC-FEED-03, AC-DUP-01, AC-DUP-05.

**REQ-FEED-03 - Event order is commit order.**
Once any reader can see the event with ID `N`, no event with an ID lower than `N` may become visible later.
A consumer that advances its cursor to the last event it has seen therefore never skips an event.
Verified by: AC-FEED-04.

**REQ-FEED-04 - Events are immutable.**
An event's content MUST NOT change after it is appended, and events MUST NOT be deleted.
Verified by: AC-FEED-02, AC-FEED-05.

**REQ-FEED-05 - A demonstration consumer.**
A `consume-feed` command MUST poll the feed, print one line per `order.accepted` event to stdout, persist its cursor only after the event's line has been flushed, and resume from that cursor after a restart.
Verified by: AC-FEED-07, D-04, D-09.

### HTTP contract

**REQ-API-01 - The implementation conforms to the committed contract.**
Every response to an operation that [openapi.yaml](openapi.yaml) declares MUST have a status code declared for that operation and a body that validates against the schema declared for that status code.
Every response to a path or method that the document does not declare, other than the documentation routes `/openapi.json` and `/docs`, MUST be `application/problem+json` and validate against the document's `Problem` schema.
The service MUST publish that document as its own OpenAPI description.
Verified by: AC-API-01, AC-API-05.

**REQ-API-02 - One error shape.**
Every error response, including those produced by the web framework for unknown routes and methods, MUST use the `application/problem+json` shape defined in [04-api.md](04-api.md#errors).
Verified by: AC-API-02, AC-API-04, AC-ORD-03, AC-ORD-05, AC-ORD-07, AC-STK-04, AC-FEED-06.

**REQ-API-03 - Database unavailability is reported, not hidden.**
When the database cannot be reached, a connection cannot be obtained in time, or a statement times out, the API MUST respond `503` with problem code `service_unavailable` and a `Retry-After` header.
The API process MUST start even if the database is unreachable.
Verified by: AC-API-03.

### Operations

**REQ-OPS-01 - PostgreSQL holds all state that matters.**
Correctness MUST NOT depend on in-memory state.
Any process MAY be killed at any instant and restarted without losing or duplicating an effect.
Verified by: AC-OUT-04, AC-OUT-05, D-08.

**REQ-OPS-02 - Local run without containers.**
The system MUST run on macOS and Linux with Python and PostgreSQL installed natively.
Docker Compose MAY be offered to provision PostgreSQL only.
Verified by: inspection of the README run instructions, D-01.

**REQ-OPS-03 - Versioned schema migrations.**
The database schema MUST be created and changed only by versioned SQL migrations, applied by one command.
Applying migrations twice MUST be a no-op.
Verified by: AC-OPS-01.

**REQ-OPS-04 - A burst command with duplicates.**
A `burst` command MUST submit a fixed set of orders concurrently over HTTP, including identical duplicates of the same `order_ref` and one conflicting resubmission, and MUST print a per-`order_ref` summary.
Verified by: AC-OPS-03, D-03.

**REQ-OPS-05 - A seed command.**
A `seed` command MUST create the demo catalogue and initial stock levels.
Running it again MUST NOT change existing products or stock levels.
It MUST only insert: it never updates, deletes or truncates anything.
Verified by: AC-OPS-02.

### Structure

**REQ-ARCH-01 - Component dependencies are enforced.**
Imports between components MUST follow the dependency rules in [03-architecture.md](03-architecture.md#dependency-rules), and a check in the test suite MUST fail when they do not.
Verified by: AC-ARCH-01.

## Unhappy path 1: duplicate submissions

A duplicate submission is a `POST /orders` whose `order_ref` equals that of an order that already exists or is being accepted concurrently.
Two submissions have the same content when their `customer_id` values are equal and their items form the same set of `(sku, qty)` pairs, regardless of item order.

**REQ-DUP-01 - An identical resubmission is a no-op that returns the order.**
It MUST respond `200 OK` with the current representation of the existing order.
It MUST NOT create an order, append an event, change stock, or reprice the order.
Verified by: AC-DUP-01, AC-DUP-02, AC-DUP-05, AC-ORD-09.

**REQ-DUP-02 - A conflicting resubmission is rejected.**
A resubmission with different content MUST respond `409 Conflict` with problem code `order_ref_conflict` and MUST NOT change the existing order.
Verified by: AC-DUP-03, AC-DUP-07.

**REQ-DUP-03 - Concurrent submissions produce one order.**
Any number of concurrent submissions with the same `order_ref` MUST result in exactly one accepted order and one event.
When they all have the same content, exactly one MUST receive `201` and all others `200`.
Verified by: AC-DUP-04, AC-DUP-08, AC-OPS-03.

**REQ-DUP-04 - A retry sees the original outcome.**
The response to a resubmission MUST depend only on the stored order, never on the current catalogue.
A retry after a price change returns the original total, and a retry is answered before any catalogue validation.
Verified by: AC-DUP-06, AC-DUP-07.

## Unhappy path 2: stock-processing outage and catch-up

The stock applier is the Inventory process that turns `order.accepted` events into stock decrements.
An outage is any period in which it is not running: stopped, crashed, or paused by an operator.

**REQ-OUT-01 - Orders are accepted during an outage.**
While the stock applier is not running, `POST /orders` MUST keep accepting orders and `GET /order-events` MUST keep serving their events.
Those orders stay `accepted`, `on_hand` does not change, and `as_of_event_id` does not advance.
Verified by: AC-OUT-01, AC-ORD-08, D-07.

**REQ-OUT-02 - Stock catches up after recovery.**
When the stock applier runs again, it MUST apply every event appended during the outage without operator action.
Once it has caught up, every SKU's `on_hand` MUST equal the value it would have had with no outage.
Verified by: AC-OUT-02, AC-OUT-03, D-08.

**REQ-OUT-03 - No partial effects on a crash.**
If the stock applier stops at any instant, including in the middle of a batch, no partial stock or status change may persist, and after restart every event MUST be applied exactly once.
Verified by: AC-OUT-04, AC-OUT-05.

**REQ-OUT-04 - Overlapping appliers are safe.**
Running two stock appliers at the same time, for example during a restart overlap, MUST NOT apply any event twice.
Verified by: AC-OUT-06.

## Non-goals

Each non-goal is a deliberate omission with a reason.
Building any of them is a scope change that starts with a change to this document.

**NG-01 - Authentication and authorisation.**
All endpoints are open.
Nothing in the design depends on caller identity, so an auth layer can be added in front without changing the contract.

**NG-02 - Edge cases beyond the two unhappy paths.**
There is no order cancellation, amendment, payment, refund or expiry, and no state for them.
The order state machine has exactly the two states the stock flow needs.

**NG-03 - Reporting.**
There is no daily report or aggregate reporting endpoint.
The integration surface is the order-accepted feed.

**NG-04 - Catalogue and stock management API.**
Products and initial stock are created by the `seed` command.
There is no endpoint to create products, change prices, restock or adjust stock.

**NG-05 - Reservation and fulfilment.**
Stock is decremented, not reserved.
There is no allocation, picking, shipping or warehouse model.
See [ADR-0006](adr/0006-asynchronous-stock-decrement.md).

**NG-06 - Multi-currency, tax, discounts and rounding.**
The system has one currency and stores integer minor units; no calculation divides, so nothing rounds.

**NG-07 - Order listing and search.**
Orders are fetched by `order_ref` only, so no orders endpoint needs pagination.

**NG-08 - Push delivery.**
The feed is pull-only: no webhooks, server-sent events or long polling.
See [ADR-0007](adr/0007-integration-surface-pull-feed.md).

**NG-09 - Event retention and compaction.**
The event log grows without bound; archiving is left for when volume requires it.

**NG-10 - Exactly-once delivery to external consumers.**
The feed offers at-least-once delivery; consumers deduplicate by `event_id`.

**NG-11 - Parallel stock application.**
Exactly one stock applier is active at a time.
Its throughput is far above what a single-node intake can produce, and serial application keeps ordering and reasoning simple.

**NG-12 - Tolerating PostgreSQL unavailability.**
PostgreSQL is the system of record.
While it is down, the API answers `503` and the stock applier retries; there is no replica, failover or local buffering.

**NG-13 - Observability beyond logs.**
There are no metrics, traces or health endpoints.
Processes log to stderr, and consumer lag is visible through `as_of_event_id`.

**NG-14 - Rate limiting and multi-tenancy.**
Request sizes are bounded by the schema only.

**NG-15 - Containerising the application.**
The processes run natively.
Docker Compose, where used, provisions PostgreSQL only.

**NG-16 - A separate idempotency key.**
`order_ref` is the idempotency key; there is no `Idempotency-Key` header.
See [ADR-0005](adr/0005-order-ref-idempotency.md).

## Coverage

Every requirement above lists its verification.
The reverse mapping, from each acceptance scenario to its requirements, is in [06-acceptance.md](06-acceptance.md#traceability-matrix).
