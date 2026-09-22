# 06 - Acceptance scenarios

Each scenario below becomes one test, or one parametrized test for a scenario outline, in the next implementation step.
Values are concrete so that a scenario translates into assertions without interpretation.
The demonstration walkthrough for the recorded demo is [07-demo.md](07-demo.md).

## Test conventions

- **Naming.**
  Each test function starts with its scenario ID in lower case, for example `test_ac_dup_04_concurrent_identical_submissions_create_one_order`.
  Tests live in the module named in [03-architecture.md](03-architecture.md#source-layout) for their group.
- **Real PostgreSQL.**
  Tests run against the database at `ORDERS_STOCK_TEST_DATABASE_URL`.
  The suite refuses to start unless the database name ends in `_test`.
  Nothing mocks the database.
- **Schema.**
  Once per session, the suite drops and recreates schema `public` and applies the migrations with the same code as `orders-stock migrate`.
  This needs the `orders_stock` role to own `public` in the test database, which the setup in [03-architecture.md](03-architecture.md#running-locally) arranges.
- **Isolation.**
  Before each test: `TRUNCATE order_events, order_items, orders, stock_levels, products RESTART IDENTITY`, set the `stock-applier` offset to 0, then seed the demo catalogue unless the scenario says otherwise.
- **`client` fixture.**
  A FastAPI `TestClient` over `create_app()` configured for the test database.
- **`live_api` fixture.**
  Uvicorn serving `create_app()` in a background thread on `127.0.0.1` and a free port, for scenarios that need real concurrent HTTP or a subprocess client.
- **Stock applier.**
  Tests apply stock in-process by calling `apply_next_batch`.
  "Drain the applier" means calling it until it returns `None`.
  "The applier is stopped" means it is not called.
  Only AC-OUT-05 runs the real `stock-worker` process.
- **Commands.**
  Scenarios that run an `orders-stock` command run it as a subprocess with `ORDERS_STOCK_DATABASE_URL` set to the test database and, where needed, `ORDERS_STOCK_API_URL` set to the `live_api` server.
- **Contract checking.**
  Every HTTP response a test receives from the API, through any `TestClient` or through `live_api`, is checked against [openapi.yaml](openapi.yaml) as AC-API-01 specifies when it is received, so a violation fails the test during which it occurred.
  Each checked response is also recorded as an observed pair of operation and status code.
  A `pytest_collection_modifyitems` hook in `tests/conftest.py` moves the AC-API-01 test to the end of the collected items, so it runs after every test that produces responses.
  Its coverage clause, that every declared pair was observed, is asserted only when the run selects the whole suite, with no path, `-k`, `-m` or deselection narrowing it, because a narrower run cannot observe every pair.
  The per-response clauses apply in every run.
- **Fault injection.**
  Where a scenario injects a fault, the test replaces one of the functions below with `pytest`'s `monkeypatch`.
  The implementation keeps them as module-level names and calls them through their module, so a replacement takes effect.

| Seam | Contract | Used by |
| --- | --- | --- |
| `orders_stock.orders.events.append_event` | Takes the append lock and inserts the event: steps 5 and 6 of [the acceptance transaction](05-reliability.md#the-acceptance-transaction) | AC-ORD-07, AC-FEED-04 |
| `orders_stock.inventory.repository.advance_offset` | Step 5 of [the stock application transaction](05-reliability.md#the-stock-application-transaction) | AC-OUT-04 |
| `orders_stock.orders.service.accept_order`, `orders_stock.orders.service.get_order`, `orders_stock.orders.events.read_events`, `orders_stock.inventory.repository.get_stock` | The component function behind each HTTP operation | AC-API-04 |

### Background

Unless a scenario says otherwise:

- The demo catalogue from [03-architecture.md](03-architecture.md#seed) is seeded: `APL-003` at 349 with 40 on hand, `BAN-001` at 199 with 50, `BRD-004` at 425 with 20, `MLK-002` at 115 with 30.
- There are no orders and no events, and the `stock-applier` offset is 0.
- **O1** denotes the request `{"order_ref": "web-100045", "customer_id": "cust-42", "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}]}`, whose total is 747.

## Orders

### AC-ORD-01 - Accept a new order

Traces: REQ-ORD-01, REQ-ORD-02, REQ-ORD-07, REQ-CAT-01.

- **Given** the background
- **When** O1 is posted to `/orders`
- **Then** the status is `201` and `Location` is `/orders/web-100045`
- **And** the body has `order_ref` `web-100045`, `customer_id` `cust-42`, `status` `accepted`, `total_cents` 747, `stock_committed_at` null, and `items` exactly `[{"sku": "APL-003", "qty": 1, "unit_price_cents": 349, "line_total_cents": 349}, {"sku": "BAN-001", "qty": 2, "unit_price_cents": 199, "line_total_cents": 398}]`
- **And** `accepted_at` is an RFC 3339 timestamp ending in `Z`
- **And** the database holds 1 order, 2 order items, and 1 event with `event_type` `order.accepted`, `event_version` 1, and `payload` equal to the body without `status` and `stock_committed_at`
- **And** `on_hand` is still 50 for `BAN-001` and 40 for `APL-003`.

### AC-ORD-02 - Fetch an order

Traces: REQ-ORD-03.

- **Given** O1 was accepted with response body B
- **When** `GET /orders/web-100045`
- **Then** the status is `200` and the body equals B.

### AC-ORD-03 - Fetch an unknown order

Traces: REQ-ORD-03, REQ-API-02.

- **When** `GET /orders/web-999999`
- **Then** the status is `404`, the content type is `application/problem+json`, `code` is `order_not_found`, `status` is 404, and `type` ends with `04-api.md#order_not_found`.

### AC-ORD-04 - Prices are fixed at acceptance

Traces: REQ-ORD-02.

- **Given** O1 was accepted
- **And** the price of `BAN-001` is then changed to 249 directly in the database
- **When** `{"order_ref": "web-100046", "customer_id": "cust-7", "items": [{"sku": "BAN-001", "qty": 1}]}` is posted
- **Then** `web-100046` has `unit_price_cents` 249 and `total_cents` 249
- **And** `GET /orders/web-100045` still shows `unit_price_cents` 199 for `BAN-001` and `total_cents` 747.

### AC-ORD-05 - Malformed requests are rejected without effect

Traces: REQ-ORD-04, REQ-API-02.

Scenario outline.

- **When** O1, modified as in the row, is posted
- **Then** the status is `422`, `code` is `validation_error`, and `errors` contains an entry whose `pointer` is the row's pointer
- **And** no order, item or event exists.

| Case | Modification of O1 | Pointer |
| --- | --- | --- |
| missing `order_ref` | remove `order_ref` | `#/order_ref` |
| missing `customer_id` | remove `customer_id` | `#/customer_id` |
| `order_ref` with a space | `order_ref` = `"web 100045"` | `#/order_ref` |
| `order_ref` too long | `order_ref` = 65 times `"a"` | `#/order_ref` |
| no items | `items` = `[]` | `#/items` |
| zero quantity | `items[0].qty` = 0 | `#/items/0/qty` |
| quantity too large | `items[0].qty` = 10001 | `#/items/0/qty` |
| quantity as a string | `items[0].qty` = `"2"` | `#/items/0/qty` |
| fractional quantity | `items[0].qty` = 2.5 | `#/items/0/qty` |
| repeated SKU | `items` = `[{"sku": "BAN-001", "qty": 1}, {"sku": "BAN-001", "qty": 2}]` | `#/items/1/sku` |
| unknown member | add `"quantity": 2` to `items[0]` | `#/items/0/quantity` |
| body is not JSON | raw body `{"order_ref":` | `#` |
| body is not UTF-8 | raw body `{"order_ref":"café"}` encoded in Latin-1 | `#` |

Request models validate strictly: a string such as `"2"` is not coerced to an integer.

### AC-ORD-06 - Unknown SKU

Traces: REQ-ORD-05.

- **When** `{"order_ref": "web-100051", "customer_id": "cust-42", "items": [{"sku": "BAN-001", "qty": 1}, {"sku": "XYZ-999", "qty": 1}]}` is posted
- **Then** the status is `422`, `code` is `unknown_sku`, and `errors` is exactly one entry with `pointer` `#/items/1/sku` and a `detail` containing `XYZ-999`
- **And** no order, item or event exists.

### AC-ORD-07 - The order and its event are atomic

Traces: REQ-ORD-07, REQ-API-02.

- **Given** the Orders function that inserts the event is replaced by one that raises `RuntimeError`, so the failure happens after the order and items were inserted in the same transaction
- **When** O1 is posted
- **Then** the status is `500` and `code` is `internal_error`
- **And** no order, item or event exists
- **When** the fault is removed and O1 is posted again
- **Then** the status is `201`.

### AC-ORD-08 - Intake does not touch Inventory

Traces: REQ-ORD-06, REQ-OUT-01.

- **Given** a separate connection has run `BEGIN; LOCK TABLE stock_levels, consumer_offsets IN ACCESS EXCLUSIVE MODE;` and keeps the transaction open
- **When** O1 is posted
- **Then** the status is `201` within 2 seconds
- **And** the locking transaction is then rolled back.

### AC-ORD-09 - Items are in code-point SKU order in every representation

Traces: REQ-ORD-01, REQ-ORD-03, REQ-DUP-01, REQ-FEED-01.

- **Given** the product `apl-001` at 99 with 10 on hand is added directly in the database
- **When** `{"order_ref": "web-100052", "customer_id": "cust-42", "items": [{"sku": "apl-001", "qty": 1}, {"sku": "BAN-001", "qty": 1}]}` is posted with response body B
- **Then** B lists `BAN-001` before `apl-001`, the code-point order, which a case-insensitive collation such as `en_US.utf8` would reverse
- **And** `GET /orders/web-100052` and a resubmission of the same request each return a body equal to B
- **And** the order's event `data` equals B without `status` and `stock_committed_at`.

## Duplicate submissions (unhappy path 1)

### AC-DUP-01 - An identical resubmission returns the existing order

Traces: REQ-DUP-01, REQ-FEED-02.

- **Given** O1 was accepted with response body B
- **When** O1 is posted again
- **Then** the status is `200` and the body equals B
- **And** the database holds 1 order and 1 event
- **When** the applier is drained
- **Then** `on_hand` is 48 for `BAN-001` and 39 for `APL-003`.

### AC-DUP-02 - Item order does not matter

Traces: REQ-DUP-01.

- **Given** O1 was accepted
- **When** O1 is posted again with its items in reverse order
- **Then** the status is `200`.

### AC-DUP-03 - A conflicting resubmission is rejected

Traces: REQ-DUP-02.

Scenario outline.

- **Given** O1 was accepted with response body B
- **When** O1, modified as in the row, is posted
- **Then** the status is `409` and `code` is `order_ref_conflict`
- **And** `GET /orders/web-100045` returns B, and the database holds 1 event.

| Modification of O1 |
| --- |
| `BAN-001` quantity 3 instead of 2 |
| `customer_id` = `"cust-43"` |
| an additional item `{"sku": "MLK-002", "qty": 1}` |
| the `APL-003` item removed |

### AC-DUP-04 - Concurrent identical submissions create one order

Traces: REQ-DUP-03, REQ-STK-02.

- **Given** the `live_api` fixture
- **When** 20 threads, released together by a barrier, each post O1
- **Then** exactly 1 response is `201` and 19 are `200`
- **And** all 20 bodies have `order_ref` `web-100045`, the same items, and `total_cents` 747
- **And** the database holds 1 order and 1 event
- **When** the applier is drained
- **Then** `on_hand` is 48 for `BAN-001` and 39 for `APL-003`.

### AC-DUP-05 - A resubmission after stock was committed

Traces: REQ-DUP-01, REQ-FEED-02.

- **Given** O1 was accepted and the applier drained
- **When** O1 is posted again
- **Then** the status is `200`, `status` is `stock_committed`, and `stock_committed_at` is set
- **And** the database holds 1 event
- **When** the applier is drained again
- **Then** `on_hand` is still 48 for `BAN-001`.

### AC-DUP-06 - A retry is never repriced

Traces: REQ-DUP-04.

- **Given** O1 was accepted
- **And** the price of `BAN-001` is then changed to 249 directly in the database
- **When** O1 is posted again
- **Then** the status is `200`, `BAN-001` has `unit_price_cents` 199, and `total_cents` is 747.

### AC-DUP-07 - An existing order_ref is resolved before catalogue validation

Traces: REQ-DUP-02, REQ-DUP-04.

- **Given** O1 was accepted
- **When** `{"order_ref": "web-100045", "customer_id": "cust-42", "items": [{"sku": "XYZ-999", "qty": 1}]}` is posted
- **Then** the status is `409` with `code` `order_ref_conflict`, not `422`.

### AC-DUP-08 - Concurrent submissions with different content

Traces: REQ-DUP-03.

- **Given** the `live_api` fixture
- **When** 10 threads, released together by a barrier, post `web-100045` for `cust-42`: 5 with O1's items and 5 with `[{"sku": "BAN-001", "qty": 3}]`
- **Then** exactly 1 response is `201`
- **And** the stored order's items equal the items of the request that received `201`
- **And** every other response is `200` if its items equal the stored items, and `409` otherwise
- **And** the database holds 1 order and 1 event.

## Stock

### AC-STK-01 - The stock applier applies an accepted order

Traces: REQ-STK-02, REQ-STK-03.

- **Given** O1 was accepted as event 1
- **When** `apply_next_batch` runs once
- **Then** it reports `first_event_id` 1, `last_event_id` 1, `orders_committed` 1, `orders_skipped` 0
- **And** `on_hand` is 48 for `BAN-001` and 39 for `APL-003`, and unchanged for the other SKUs
- **And** `web-100045` has `status` `stock_committed` and a `stock_committed_at` not earlier than its `accepted_at`
- **And** the `stock-applier` offset is 1.

### AC-STK-02 - One batch aggregates several orders

Traces: REQ-STK-02.

- **Given** these orders were accepted as events 1 to 3: `web-100045` (`BAN-001` x2, `APL-003` x1), `web-100047` (`BRD-004` x1, `BAN-001` x1), `web-100049` (`BAN-001` x3)
- **When** `apply_next_batch` runs once with batch size 100
- **Then** it reports `orders_committed` 3
- **And** `on_hand` is 44 for `BAN-001`, 39 for `APL-003` and 19 for `BRD-004`
- **And** all three orders are `stock_committed` and the offset is 3.

### AC-STK-03 - Insufficient stock produces a backorder

Traces: REQ-STK-04.

- **Given** `on_hand` of `BRD-004` is set to 2 directly in the database
- **When** `{"order_ref": "web-100060", "customer_id": "cust-7", "items": [{"sku": "BRD-004", "qty": 5}]}` is posted
- **Then** the status is `201`
- **When** the applier is drained
- **Then** `GET /stock/BRD-004` returns `on_hand` -3
- **And** `web-100060` is `stock_committed`.

### AC-STK-04 - Read the stock of a SKU

Traces: REQ-STK-05, REQ-STK-01, REQ-API-02.

- **Given** O1 was accepted and the applier drained
- **When** `GET /stock/BAN-001`
- **Then** the status is `200` and the body is exactly `{"sku": "BAN-001", "on_hand": 48, "as_of_event_id": 1}`
- **When** `GET /stock/XYZ-999`
- **Then** the status is `404` and `code` is `sku_not_found`.

### AC-STK-05 - Stock is conserved

Traces: REQ-STK-02, REQ-ORD-02.

- **Given** the six orders of the `burst` order set with prefix `web` were accepted and the applier drained
- **Then** for every SKU, `on_hand` equals its initial value minus the sum of `qty` over the items of `stock_committed` orders, computed by SQL from `order_items`
- **And** concretely `on_hand` is 37 for `APL-003`, 44 for `BAN-001`, 17 for `BRD-004` and 25 for `MLK-002`
- **And** every order's `total_cents` equals the sum of its items' `line_total_cents`.

## Stock applier outage (unhappy path 2)

### AC-OUT-01 - Orders are accepted while the stock applier is stopped

Traces: REQ-OUT-01, REQ-STK-05.

- **Given** O1 was accepted as event 1 and the applier drained
- **And** the applier is stopped
- **When** these orders are posted: `web-100046` (`cust-7`, `MLK-002` x4), `web-100047` (`cust-42`, `BRD-004` x1, `BAN-001` x1), `web-100048` (`cust-13`, `APL-003` x2, `MLK-002` x1)
- **Then** each response is `201`
- **And** `GET /orders/{order_ref}` shows `status` `accepted` for each of them
- **And** `GET /stock/BAN-001` returns `{"sku": "BAN-001", "on_hand": 48, "as_of_event_id": 1}`
- **And** `GET /order-events?after=1` returns events 2, 3 and 4 for `web-100046`, `web-100047` and `web-100048`, in that order.

### AC-OUT-02 - Stock catches up when the applier resumes

Traces: REQ-OUT-02, REQ-STK-05.

- **Given** the state at the end of AC-OUT-01
- **When** the applier is drained
- **Then** `web-100046`, `web-100047` and `web-100048` are `stock_committed`
- **And** `on_hand` is 37 for `APL-003`, 47 for `BAN-001`, 19 for `BRD-004` and 25 for `MLK-002`
- **And** `GET /stock/BAN-001` returns `as_of_event_id` 4.

### AC-OUT-03 - The outage does not change the outcome

Traces: REQ-OUT-02.

- **Given** run A accepts the six orders of the `burst` order set one at a time, draining the applier after each
- **And** run B, after the database is reset as between tests, accepts the same six orders with the applier stopped, then drains it once
- **Then** `on_hand` for every SKU is identical after run A and run B, and equals the values in AC-STK-05.

### AC-OUT-04 - A crash in the middle of a batch leaves no partial effect

Traces: REQ-OUT-03, REQ-STK-03, REQ-OPS-01.

- **Given** O1 and `web-100046` (`cust-7`, `MLK-002` x4) were accepted as events 1 and 2
- **And** a fault makes the stock application transaction raise after its stock updates and before its offset update
- **When** `apply_next_batch` runs once
- **Then** it raises
- **And** `on_hand` is still 50 for `BAN-001`, 40 for `APL-003` and 30 for `MLK-002`, both orders are `accepted`, and the offset is 0
- **When** the fault is removed and the applier is drained
- **Then** `on_hand` is 48 for `BAN-001`, 39 for `APL-003` and 26 for `MLK-002`, both orders are `stock_committed`, and the offset is 2
- **When** the offset is set back to 0 directly in the database and the applier is drained again
- **Then** it reports `orders_committed` 0 and `orders_skipped` 2, and `on_hand` and statuses are unchanged.

### AC-OUT-05 - A killed stock-worker process recovers without double-applying

Traces: REQ-OUT-03, REQ-OPS-01.

- **Given** O1 was accepted as event 1
- **And** a test connection holds `SELECT 1 FROM stock_levels WHERE sku = 'APL-003' FOR UPDATE` in an open transaction
- **When** `orders-stock stock-worker` is started as a subprocess
- **And** `pg_stat_activity` shows its session, `application_name` `orders-stock-stock-worker`, waiting with `wait_event_type` `Lock`; it has locked the offset and transitioned the order, and is blocked on the `APL-003` row
- **And** the test records that session's `pid`
- **And** the subprocess is killed with `SIGKILL`
- **And** the test connection rolls back
- **Then** within 5 seconds `pg_stat_activity` has no session with the recorded `pid`
- **And** `on_hand` is 50 for `BAN-001` and 40 for `APL-003`, `web-100045` is `accepted`, and the offset is 0
- **When** a new `stock-worker` subprocess runs until the offset is 1, then receives `SIGTERM`
- **Then** it exits with status 0 after logging `stock-worker stopped: offset=1`
- **And** `on_hand` is 48 for `BAN-001` and 39 for `APL-003`, and `web-100045` is `stock_committed`.

Every `pg_stat_activity` query in this scenario filters by `datname = current_database()`: the view is cluster-wide, and a `stock-worker` connected to another database, such as the demo's, has the same `application_name`.

### AC-OUT-06 - Overlapping appliers apply each order once

Traces: REQ-OUT-04.

- **Given** 50 orders `web-200001` to `web-200050`, each `{"customer_id": "cust-1", "items": [{"sku": "BAN-001", "qty": 1}]}`, were accepted as events 1 to 50
- **When** two threads each call `apply_next_batch` with batch size 5 on their own connections until it returns `None`
- **Then** `on_hand` of `BAN-001` is 0, all 50 orders are `stock_committed`, and the offset is 50
- **And** the sum of `orders_committed` over both threads is 50, and the sum of `orders_skipped` is 0.

## Integration surface

### AC-FEED-01 - Pagination

Traces: REQ-FEED-01.

- **Given** five orders `web-300001` to `web-300005` were accepted as events 1 to 5
- **Then** each request returns the listed page:

| Request | `event_id`s | `next_after` | `has_more` |
| --- | --- | --- | --- |
| `GET /order-events?after=0&limit=2` | 1, 2 | 2 | true |
| `GET /order-events?after=2&limit=2` | 3, 4 | 4 | true |
| `GET /order-events?after=4&limit=2` | 5 | 5 | false |
| `GET /order-events?after=5` | none | 5 | false |
| `GET /order-events` | 1 to 5 | 5 | false |

### AC-FEED-02 - An event is an immutable snapshot of the accepted order

Traces: REQ-FEED-01, REQ-FEED-04.

- **Given** O1 was accepted with response body B
- **When** `GET /order-events`
- **Then** the single event has `event_id` 1, `event_type` `order.accepted`, `event_version` 1, and `occurred_at` equal to B's `accepted_at`
- **And** its `data` equals B without `status` and `stock_committed_at`
- **When** the applier is drained and `GET /order-events` is repeated
- **Then** the event is byte-for-byte identical to the first read.

### AC-FEED-03 - Only accepted orders produce events

Traces: REQ-FEED-02.

- **Given** O1 was accepted
- **When** O1 is posted again, a conflicting variant of `web-100045` is posted, AC-ORD-06's unknown-SKU order is posted, and a body with `qty` 0 is posted
- **Then** the responses are `200`, `409`, `422` and `422`
- **And** the log still holds exactly 1 event.

### AC-FEED-04 - Events become visible in commit order

Traces: REQ-FEED-03.

- **Given** acceptance A of O1 runs in a thread whose Orders event-append function is wrapped to block on a `threading.Event` after inserting the event, so A holds the append lock uncommitted
- **And** acceptance B of `web-100046` (`cust-7`, `MLK-002` x4) is started in a second thread
- **When** the feed is read with `after=0`
- **Then** it returns no events
- **And** `pg_locks` shows one ungranted lock of type `advisory` whose `database` is the OID of the test database
- **When** A is released and both threads finish
- **Then** the feed returns the event of `web-100045` and then the event of `web-100046`, with ascending `event_id`s.

### AC-FEED-05 - The event log is append-only

Traces: REQ-FEED-04.

- **Given** O1 was accepted
- **When** `UPDATE order_events SET payload = '{}'` runs directly in the database
- **Then** it fails with an error whose message contains `append-only`
- **When** `DELETE FROM order_events` runs directly in the database
- **Then** it fails with an error whose message contains `append-only`.

### AC-FEED-06 - Invalid feed parameters

Traces: REQ-FEED-01, REQ-API-02.

Scenario outline.

- **When** `GET /order-events` with the row's query
- **Then** the status is `422`, `code` is `validation_error`, and `errors` contains an entry whose `parameter` is the row's parameter.

| Query | Parameter |
| --- | --- |
| `after=-1` | `after` |
| `limit=0` | `limit` |
| `limit=501` | `limit` |
| `limit=abc` | `limit` |

### AC-FEED-07 - The demonstration consumer prints, persists and resumes

Traces: REQ-FEED-05.

- **Given** the `live_api` fixture, and orders `web-300001` to `web-300003` accepted as events 1 to 3
- **When** `orders-stock consume-feed --cursor-file <tmp>/cursor` runs as a subprocess with `ORDERS_STOCK_API_URL` set, until it has printed 3 lines, and then receives `SIGINT`
- **Then** its stdout is 3 lines in the format of [03-architecture.md](03-architecture.md#consume-feed), for `event_id` 1, 2 and 3 in order
- **And** the cursor file contains `3`
- **When** orders `web-300004` and `web-300005` are accepted and the command runs again with the same cursor file until it has printed 2 lines
- **Then** it prints exactly the lines for events 4 and 5
- **When** it runs with `--from-start` until it has printed 5 lines
- **Then** it prints events 1 to 5.

## HTTP contract

### AC-API-01 - Every response conforms to the committed contract

Traces: REQ-API-01.

- **Given** every HTTP response a test receives from the API during the test session
- **Then** each response to an operation declared in [openapi.yaml](openapi.yaml) has a status code declared for that operation, and its body validates against the declared schema for that status and content type
- **And** each response to an undeclared path or method, other than `/openapi.json` and `/docs` (AC-API-05), has content type `application/problem+json` and a body that validates against `components/schemas/Problem`
- **And** when the run selects the whole suite, every declared pair of operation and status code has been observed at least once by the time this test runs, which is last, as [Contract checking](#test-conventions) describes.

### AC-API-02 - Framework errors use the problem shape

Traces: REQ-API-02.

- **When** `GET /nope`
- **Then** the status is `404`, the content type is `application/problem+json`, and `code` is `not_found`
- **When** `DELETE /orders`
- **Then** the status is `405` and `code` is `method_not_allowed`.

### AC-API-03 - An unreachable database yields 503

Traces: REQ-API-03.

- **Given** an app created by `create_app` with settings whose `database_url` points at a local port where nothing listens and whose `database_pool_timeout` is 1 second
- **Then** the app starts
- **And** each of `POST /orders` with O1, `GET /orders/web-100045`, `GET /stock/BAN-001` and `GET /order-events` returns `503` with `code` `service_unavailable` and `Retry-After: 1`.

### AC-API-04 - Unexpected errors yield 500 without leaking detail

Traces: REQ-API-02.

- **Given** the component function behind each operation is replaced by one that raises `RuntimeError("boom-7f3a")`
- **Then** each of the four operations returns `500` with `code` `internal_error`
- **And** no response body contains `boom-7f3a`.

### AC-API-05 - The service publishes the committed contract

Traces: REQ-API-01.

- **When** `GET /openapi.json`
- **Then** the body equals [openapi.yaml](openapi.yaml) parsed as YAML
- **And** `GET /docs` returns `200` with an HTML page.

## Operations

### AC-OPS-01 - Migrations

Traces: REQ-OPS-03.

- **Given** an empty database: the test drops and recreates schema `public`
- **When** `orders-stock migrate` runs
- **Then** it exits 0 and prints `applied 1 migration(s)`
- **And** the tables `products`, `orders`, `order_items`, `order_events`, `stock_levels` and `consumer_offsets` exist, together with every named constraint in [02-domain-model.md](02-domain-model.md#ddl) and the trigger `order_events_append_only`
- **When** `orders-stock migrate` runs again
- **Then** it exits 0 and prints `applied 0 migration(s)`.

### AC-OPS-02 - Seeding

Traces: REQ-OPS-05, REQ-CAT-01, REQ-STK-01.

- **Given** a migrated, empty database
- **When** `orders-stock seed` runs
- **Then** it exits 0, prints the catalogue table of [03-architecture.md](03-architecture.md#seed), and prints `seed: inserted 4 product(s)`
- **Given** O1 was then accepted and the applier drained
- **When** `orders-stock seed` runs again
- **Then** it prints `seed: inserted 0 product(s)`, `BAN-001` still has `on_hand` 48, `web-100045` and its event still exist, and the offset is still 1.

### AC-OPS-03 - The burst command

Traces: REQ-OPS-04, REQ-DUP-03.

- **Given** the `live_api` fixture
- **When** `orders-stock burst` runs as a subprocess with `ORDERS_STOCK_API_URL` set
- **Then** it exits 0
- **And** its stdout table has the rows of [03-architecture.md](03-architecture.md#burst): for each `order_ref`, the same `submitted`, `201`, `200`, `409` and `total_cents` values
- **And** its last line is exactly `summary: submitted=11 created=6 duplicate=4 conflict=1 failed=0`
- **And** the database holds 6 orders and 6 events
- **When** `orders-stock burst` runs again
- **Then** its last line is exactly `summary: submitted=11 created=0 duplicate=10 conflict=1 failed=0`
- **And** the database still holds 6 orders and 6 events.

## Structure

### AC-ARCH-01 - Component dependency rules hold

Traces: REQ-ARCH-01.

- **When** the import-linter contracts in `pyproject.toml` are checked
- **Then** every contract is kept
- **And** the contracts encode each row of [03-architecture.md](03-architecture.md#dependency-rules).

## Traceability matrix

| Scenario | Requirements |
| --- | --- |
| AC-ORD-01 | REQ-ORD-01, REQ-ORD-02, REQ-ORD-07, REQ-CAT-01 |
| AC-ORD-02 | REQ-ORD-03 |
| AC-ORD-03 | REQ-ORD-03, REQ-API-02 |
| AC-ORD-04 | REQ-ORD-02 |
| AC-ORD-05 | REQ-ORD-04, REQ-API-02 |
| AC-ORD-06 | REQ-ORD-05 |
| AC-ORD-07 | REQ-ORD-07, REQ-API-02 |
| AC-ORD-08 | REQ-ORD-06, REQ-OUT-01 |
| AC-ORD-09 | REQ-ORD-01, REQ-ORD-03, REQ-DUP-01, REQ-FEED-01 |
| AC-DUP-01 | REQ-DUP-01, REQ-FEED-02 |
| AC-DUP-02 | REQ-DUP-01 |
| AC-DUP-03 | REQ-DUP-02 |
| AC-DUP-04 | REQ-DUP-03, REQ-STK-02 |
| AC-DUP-05 | REQ-DUP-01, REQ-FEED-02 |
| AC-DUP-06 | REQ-DUP-04 |
| AC-DUP-07 | REQ-DUP-02, REQ-DUP-04 |
| AC-DUP-08 | REQ-DUP-03 |
| AC-STK-01 | REQ-STK-02, REQ-STK-03 |
| AC-STK-02 | REQ-STK-02 |
| AC-STK-03 | REQ-STK-04 |
| AC-STK-04 | REQ-STK-05, REQ-STK-01, REQ-API-02 |
| AC-STK-05 | REQ-STK-02, REQ-ORD-02 |
| AC-OUT-01 | REQ-OUT-01, REQ-STK-05 |
| AC-OUT-02 | REQ-OUT-02, REQ-STK-05 |
| AC-OUT-03 | REQ-OUT-02 |
| AC-OUT-04 | REQ-OUT-03, REQ-STK-03, REQ-OPS-01 |
| AC-OUT-05 | REQ-OUT-03, REQ-OPS-01 |
| AC-OUT-06 | REQ-OUT-04 |
| AC-FEED-01 | REQ-FEED-01 |
| AC-FEED-02 | REQ-FEED-01, REQ-FEED-04 |
| AC-FEED-03 | REQ-FEED-02 |
| AC-FEED-04 | REQ-FEED-03 |
| AC-FEED-05 | REQ-FEED-04 |
| AC-FEED-06 | REQ-FEED-01, REQ-API-02 |
| AC-FEED-07 | REQ-FEED-05 |
| AC-API-01 | REQ-API-01 |
| AC-API-02 | REQ-API-02 |
| AC-API-03 | REQ-API-03 |
| AC-API-04 | REQ-API-02 |
| AC-API-05 | REQ-API-01 |
| AC-OPS-01 | REQ-OPS-03 |
| AC-OPS-02 | REQ-OPS-05, REQ-CAT-01, REQ-STK-01 |
| AC-OPS-03 | REQ-OPS-04, REQ-DUP-03 |
| AC-ARCH-01 | REQ-ARCH-01 |

REQ-OPS-02 is verified by inspection of the README and by demo step D-01; every other requirement is covered by at least one scenario above.
