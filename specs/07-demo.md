# 07 - Demo walkthrough

This is the script for the recorded demonstration: an ordered list of commands, where to run each, and the output that proves the point.
It demonstrates the happy path and exactly the two designed unhappy paths: duplicate submissions (D-03 to D-06) and a stock-worker outage with catch-up (D-07 and D-08).
Every step is backed by automated acceptance scenarios; the demo shows the same behaviour on real processes.

## Setup

Four terminals in the repository root:

- **T1** runs `api`.
- **T2** runs `stock-worker`.
- **T3** runs `consume-feed`, standing in for another team's system.
- **T4** runs one-shot commands.

`curl` is used for HTTP; `jq` is optional and only pretty-prints.
The run starts from a new database: D-01 drops and recreates it, then applies the migrations.

## Steps

### D-01 - Provision PostgreSQL and apply the schema

Traces: REQ-OPS-02, REQ-OPS-03.

In T4:

```sh
docker compose up -d --wait   # or use a native PostgreSQL; see README.md
docker compose exec postgres dropdb -U orders_stock --if-exists orders_stock
docker compose exec postgres createdb -U orders_stock orders_stock
uv sync
uv run orders-stock migrate
```

With native PostgreSQL, recreate the database with the native commands in [Starting clean](03-architecture.md#running-locally) instead of the two `docker compose exec` lines.

Expected: `applied 1 migration(s)`.

### D-02 - Seed and start the processes

Traces: REQ-OPS-05.

In T4, `uv run orders-stock seed` prints:

```text
sku      name              price_cents  on_hand
APL-003  Apples 1kg                349       40
BAN-001  Bananas 1kg               199       50
BRD-004  Sourdough 800g            425       20
MLK-002  Whole milk 1L             115       30
seed: inserted 4 product(s)
```

In T1, `uv run orders-stock api` logs `Uvicorn running on http://127.0.0.1:8000`.

In T2, `uv run orders-stock stock-worker` logs `stock-worker started: offset=0 head=0 lag=0`.

In T3, `uv run orders-stock consume-feed --from-start` logs `consume-feed: starting after event_id=0 (cursor file .feed-cursor)`.

### D-03 - A burst with concurrent duplicates

Traces: REQ-OPS-04, REQ-DUP-01, REQ-DUP-02, REQ-DUP-03.

In T4, `uv run orders-stock burst` prints:

```text
order_ref   submitted  201  200  409  total_cents
web-100045          3    1    2    0          747
web-100046          1    1    0    0          460
web-100047          3    1    1    1          624
web-100048          1    1    0    0          813
web-100049          2    1    1    0          597
web-100050          1    1    0    0          850
summary: submitted=11 created=6 duplicate=4 conflict=1 failed=0
```

T1 shows eleven `POST /orders` access-log lines: six `201`, four `200`, one `409`.
T2 shows one or more `applied events ...` lines that together cover events 1 to 6, the last ending in `offset=6 lag=0`.

Point to make: ten submissions raced in parallel, and each `order_ref` still produced exactly one order.

### D-04 - The feed carries six orders, not eleven submissions

Traces: REQ-FEED-02, REQ-FEED-05.

T3 has printed six lines, `event_id=1` to `event_id=6`, one per `order_ref`, in commit order, for example:

```text
event_id=1 type=order.accepted order_ref=web-100046 customer_id=cust-7 total_cents=460 items=MLK-002x4
```

### D-05 - Order and stock after the burst

Traces: REQ-ORD-03, REQ-STK-05.

In T4:

```sh
curl -s http://127.0.0.1:8000/orders/web-100045 | jq
curl -s http://127.0.0.1:8000/stock/BAN-001
```

Expected: the order has `status` `stock_committed`, `total_cents` 747, and items `APL-003` x1 at 349 and `BAN-001` x2 at 199.
The stock read returns `{"sku":"BAN-001","on_hand":44,"as_of_event_id":6}`.

### D-06 - A client retry and a conflicting reuse, by hand

Traces: REQ-DUP-01, REQ-DUP-02.

In T4, resend `web-100045` with its items in a different order:

```sh
curl -si http://127.0.0.1:8000/orders -H 'content-type: application/json' \
  -d '{"order_ref":"web-100045","customer_id":"cust-42","items":[{"sku":"APL-003","qty":1},{"sku":"BAN-001","qty":2}]}'
```

Expected: `HTTP/1.1 200 OK` and the same order; item order does not make a different order.

Send it again with `"qty":5` for `BAN-001`.
Expected: `HTTP/1.1 409 Conflict`, `content-type: application/problem+json`, and `"code":"order_ref_conflict"`.

T3 prints nothing new, and `curl -s http://127.0.0.1:8000/stock/BAN-001` still shows `on_hand` 44.

### D-07 - Outage: kill the stock worker and keep selling

Traces: REQ-OUT-01.

In T4:

```sh
pkill -9 -f 'orders-stock stock-worker'
uv run orders-stock burst --prefix outage
curl -s http://127.0.0.1:8000/orders/outage-100045 | jq .status
curl -s http://127.0.0.1:8000/stock/BAN-001
```

Expected:

- T2 shows the worker was killed; it had no chance to shut down cleanly.
- `burst` prints `summary: submitted=11 created=6 duplicate=4 conflict=1 failed=0`: intake is unaffected.
- T3 prints six new lines, `event_id=7` to `event_id=12`: the feed does not depend on the stock worker.
- The order's status is `"accepted"`.
- The stock read returns `{"sku":"BAN-001","on_hand":44,"as_of_event_id":6}`: stale, and it says so.

### D-08 - Recovery: restart the stock worker and watch it catch up

Traces: REQ-OUT-02, REQ-OPS-01.

In T2, `uv run orders-stock stock-worker` logs:

```text
stock-worker started: offset=6 head=12 lag=6
applied events 7..12: orders=6 skus=4 offset=12 lag=0
```

In T4:

```sh
for sku in APL-003 BAN-001 BRD-004 MLK-002; do curl -s http://127.0.0.1:8000/stock/$sku; echo; done
curl -s http://127.0.0.1:8000/orders/outage-100045 | jq .status
```

Expected: `on_hand` is 34 for `APL-003`, 38 for `BAN-001`, 14 for `BRD-004` and 20 for `MLK-002`, each with `as_of_event_id` 12, and the status is `"stock_committed"`.
These are exactly the values the system would hold had the worker never stopped: the seed values minus two bursts' quantities.

### D-09 - The consumer resumes from its cursor

Traces: REQ-FEED-05.

In T3, stop the consumer with Ctrl-C and start it again with `uv run orders-stock consume-feed`.
Expected: `consume-feed: starting after event_id=12 (cursor file .feed-cursor)` and no replayed lines.

In T4:

```sh
curl -s http://127.0.0.1:8000/orders -H 'content-type: application/json' \
  -d '{"order_ref":"web-100051","customer_id":"cust-5","items":[{"sku":"MLK-002","qty":2}]}'
```

Expected: T3 prints `event_id=13 type=order.accepted order_ref=web-100051 customer_id=cust-5 total_cents=230 items=MLK-002x2`, and T2 logs `applied events 13..13: orders=1 skus=1 offset=13 lag=0`.

### D-10 - The acceptance suite

Traces: all scenarios in [06-acceptance.md](06-acceptance.md).

In T4, `uv run pytest -v` lists one or more tests per acceptance scenario, each named after its `AC-` ID, and all pass with none skipped.

## Code tour

After the live steps, the recording walks through the code in this order, one point per stop:

1. `specs/`: requirements, acceptance scenarios and decision records came first; the code follows them.
2. `migrations/0001_initial.sql`: the invariants the database enforces, including the append-only trigger.
3. `orders/service.py`, `accept_order`: `INSERT ... ON CONFLICT DO NOTHING` and the retry fast path.
4. `orders/events.py`, `append_event`: the advisory lock that makes `event_id` order equal commit order.
5. `inventory/applier.py`, `apply_next_batch`: offset lock, guarded status transition, per-SKU decrement, offset advance, one transaction.
6. `tests/test_duplicates.py`, AC-DUP-04, and `tests/test_outage.py`, AC-OUT-05: the race and the killed process, tested for real.
