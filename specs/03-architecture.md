# 03 - Architecture

This document defines the component boundaries, the processes, the source layout, the command-line interface, and the end-to-end flows.
The reasoning behind the split is recorded in [ADR-0001](adr/0001-components-and-processes.md).

## Overview

The system is a modular monolith: one Python package, `orders_stock`, containing three components that own disjoint sets of tables in one PostgreSQL database.

- **Catalogue** owns products and their prices.
- **Orders** owns orders, their items, and the event log, and serves order intake, order reads and the public feed.
- **Inventory** owns stock levels and the stock applier's offset, serves stock reads, and runs the stock applier.

The components run as two long-running processes, `api` and `stock-worker`, which share nothing but committed rows in PostgreSQL.
A third long-running process, `consume-feed`, stands in for another team's system and talks to the API over HTTP only.

```mermaid
flowchart LR
    other(["Another system<br/>consume-feed"])
    clients(["HTTP clients<br/>web, mobile, burst"])

    subgraph api["api process"]
        orders_http["Orders routes<br/>POST /orders<br/>GET /orders/{order_ref}<br/>GET /order-events"]
        stock_http["Inventory read route<br/>GET /stock/{sku}"]
    end

    subgraph pg["PostgreSQL"]
        catalogue_tables[("products")]
        orders_tables[("orders<br/>order_items<br/>order_events")]
        inventory_tables[("stock_levels<br/>consumer_offsets")]
    end

    subgraph worker["stock-worker process"]
        applier["Inventory<br/>stock applier"]
    end

    other -- "HTTP polling" --> orders_http
    clients -- HTTP --> orders_http
    clients -- HTTP --> stock_http
    orders_http -- "read prices" --> catalogue_tables
    orders_http -- "order, items and event<br/>in one transaction" --> orders_tables
    stock_http -- "read on_hand<br/>and offset" --> inventory_tables
    orders_tables <-- "read events after offset,<br/>mark stock_committed" --> applier
    inventory_tables <-- "decrement on_hand,<br/>advance offset" --> applier
```

## Components

### Catalogue

Owns `products`.
Holds reference data that intake needs to price orders.
It has no HTTP surface: products are created by `seed` (NG-04).

### Orders

Owns `orders`, `order_items` and `order_events`.
Accepts orders, collapses duplicates, prices orders through the Catalogue, appends `order.accepted` events in the accepting transaction, serves order reads, and serves the event log as the public feed.
It never calls Inventory.

### Inventory

Owns `stock_levels` and `consumer_offsets`.
Serves stock reads and runs the stock applier, which consumes the event log and applies stock decrements.
It reaches Orders only through the Orders public interface.

### Why the boundaries are drawn here

**Price and stock are separate components.**
The example product record mixes a price and a stock count, but they change for different reasons and different parties need them.
Intake needs prices to accept an order, and intake must keep working while stock processing is down (REQ-OUT-01).
If prices lived in Inventory, intake would depend on the very component whose outage it must tolerate.
With a separate Catalogue, intake depends only on reference data and on its own tables.

**Orders owns the event log.**
An `order.accepted` event is a fact about an order, created in the order's own transaction.
The component that creates the fact publishes it; this is the transactional outbox ([ADR-0003](adr/0003-transactional-outbox.md)).

**Dependencies point from Inventory to Orders, never back.**
Inventory consumes the events Orders publishes and calls one Orders command, `mark_stock_committed`.
Orders has no knowledge of Inventory, so no Inventory failure can reach intake (REQ-ORD-06).

**Inventory changes an order's status inside its own transaction.**
Status and stock must change together (REQ-STK-03).
Doing both in one transaction avoids a second event flow back to Orders and makes it impossible for status and stock to disagree.
The price is that Orders and Inventory must share a database; the evolution path below removes that coupling when it is worth paying for.
Orders keeps control of its state machine because the transition is Orders code, guarded by Orders' own condition.

## Component interfaces

Each component package exports its public interface from its `__init__.py`.
Other components import only these names; submodules such as `orders_stock.orders.repository` are private.
Orders and Inventory also export `router`, the FastAPI router of their HTTP routes, which only `orders_stock.api` includes; this is how `api` reaches the routes without importing a private submodule.
Functions take a `psycopg.Connection`.
A function marked "own transaction" opens and commits its transaction on that connection; every other function runs inside whatever transaction its caller holds.

### Catalogue interface

| Function | Semantics |
| --- | --- |
| `get_prices(conn, skus: Collection[str]) -> dict[str, int]` | Current `price_cents` for each known SKU; unknown SKUs are absent from the result. |
| `insert_missing_products(conn, products: Iterable[Product]) -> int` | Inserts products whose SKU is not present (`ON CONFLICT (sku) DO NOTHING`); returns the number inserted. |

### Orders interface

| Function | Semantics |
| --- | --- |
| `accept_order(conn, request: NewOrder) -> AcceptResult` | Own transaction. Implements [the acceptance algorithm](05-reliability.md#the-acceptance-transaction). Returns `AcceptResult(outcome: Literal["created", "duplicate"], order: Order)`. Raises `OrderRefConflict` or `UnknownSkus`. |
| `get_order(conn, order_ref: str) -> Order \| None` | The order with its items in ascending SKU order. |
| `read_events(conn, after: int, limit: int) -> list[OrderEvent]` | Up to `limit` events with `event_id > after`, ascending. Used by both the feed route and the stock applier. |
| `head_event_id(conn) -> int` | `max(event_id)`, or 0 when the log is empty. |
| `mark_stock_committed(conn, order_refs: Collection[str]) -> set[str]` | Moves the given orders from `accepted` to `stock_committed` and returns the refs that actually moved. Must run inside the caller's transaction. |

`OrderRefConflict` carries the `order_ref`; `UnknownSkus` carries the item indexes and SKUs that are unknown.

### Inventory interface

| Function | Semantics |
| --- | --- |
| `get_stock(conn, sku: str) -> StockLevel \| None` | `sku`, `on_hand` and `as_of_event_id`, read in one statement. |
| `insert_missing_stock_levels(conn, levels: Mapping[str, int]) -> int` | Inserts a stock level for each SKU that has none; returns the number inserted. |
| `apply_next_batch(conn, batch_size: int) -> AppliedBatch \| None` | Own transaction. Implements [the stock application transaction](05-reliability.md#the-stock-application-transaction). Returns `None` when there is nothing to apply. |
| `run_stock_worker(settings: Settings, stop: threading.Event) -> None` | The worker loop below. Returns after `stop` is set. |

`AppliedBatch` has `first_event_id`, `last_event_id`, `orders_committed`, `orders_skipped` and `skus_touched`.

## Dependency rules

| Module | May import |
| --- | --- |
| `orders_stock.config`, `orders_stock.db`, `orders_stock.problems` (the kernel) | Standard library and third-party packages only |
| `orders_stock.catalog` | The kernel |
| `orders_stock.orders` | `orders_stock.catalog` public interface, the kernel |
| `orders_stock.inventory` | `orders_stock.orders` public interface, the kernel |
| `orders_stock.api` | `orders_stock.orders`, `orders_stock.inventory`, the kernel |
| `orders_stock.demo.seed_data` | `orders_stock.catalog` public interface |
| `orders_stock.demo.seed` | `orders_stock.demo.seed_data`, `orders_stock.catalog`, `orders_stock.inventory`, the kernel |
| `orders_stock.demo.burst`, `orders_stock.demo.feed_consumer` | `orders_stock.config` and `httpx` only; they are HTTP clients and see the system exactly as another team would |
| `orders_stock.cli`, `orders_stock.migrate` | Anything |

No module other than `orders_stock.cli` may import `orders_stock.api`, and no module may import `orders_stock.cli`.
These rules are encoded as import-linter contracts in `pyproject.toml` and checked by AC-ARCH-01.

## Source layout

```text
.github/workflows/ci.yml    lint, format, types, dependency rules and tests against PostgreSQL 17
pyproject.toml              metadata, dependencies, and ruff, mypy, pytest and import-linter configuration
uv.lock                     locked dependency versions
.python-version             3.14
compose.yaml                optional: PostgreSQL 17 for local use
scripts/
    start.sh                runs the service locally in one terminal: checks, migrate, seed, api and stock-worker
    demo.sh                 steps through the demo walkthrough of 07-demo.md against the processes start.sh runs
docker/initdb/
    create-test-database.sql
migrations/
    0001_initial.sql        the DDL of 02-domain-model.md
src/orders_stock/
    config.py               Settings: a frozen dataclass read from the environment
    db.py                   connection pool factory and per-connection configuration
    problems.py             problem-details exception types, codes, titles and FastAPI exception handlers
    migrate.py              apply_migrations(database_url) -> int, using yoyo-migrations
    api.py                  create_app(settings): routers, exception handlers, and the committed OpenAPI document
    cli.py                  the orders-stock entry point and its subcommands
    catalog/
        __init__.py         public interface
        repository.py       SQL for products
    orders/
        __init__.py         public interface
        models.py           Pydantic request models, domain dataclasses and domain errors
        service.py          accept_order, get_order, mark_stock_committed
        repository.py       SQL for orders and order items
        events.py           append_event (append lock and insert), read_events, head_event_id
        routes.py           /orders and /order-events
    inventory/
        __init__.py         public interface
        repository.py       SQL for stock levels and the offset, including advance_offset
        applier.py          apply_next_batch and run_stock_worker
        routes.py           /stock
    demo/
        seed_data.py        the demo catalogue
        seed.py
        burst.py
        feed_consumer.py
tests/
    conftest.py             database, app, live server and subprocess fixtures, and the hook that runs AC-API-01 last
    contract.py             validates responses against specs/openapi.yaml
    test_orders.py          AC-ORD-*
    test_duplicates.py      AC-DUP-*
    test_stock.py           AC-STK-*
    test_outage.py          AC-OUT-*
    test_feed.py            AC-FEED-*
    test_api.py             AC-API-*
    test_ops.py             AC-OPS-*
    test_architecture.py    AC-ARCH-01
```

`api.py` loads `specs/openapi.yaml` once at start-up and serves it as the application's OpenAPI document; FastAPI's generated schema is not used.
The problem `type` URIs are built from one constant, `https://github.com/mbutata/orders-stock-service/blob/main/specs/04-api.md#`, followed by the code.

## Processes

| Process | Command | Runs | Holds state | Stopping it affects |
| --- | --- | --- | --- | --- |
| `api` | `orders-stock api` | Uvicorn serving `create_app(settings)`: Orders routes and the Inventory read route | None; any number of replicas is safe | HTTP clients only; the stock applier keeps draining the log |
| `stock-worker` | `orders-stock stock-worker` | The Inventory stock applier loop | Its offset, in PostgreSQL | Nothing else: orders are still accepted and the feed still serves them (REQ-OUT-01) |
| `consume-feed` | `orders-stock consume-feed` | The demonstration feed consumer | Its cursor, in a local file | Nothing else |

`migrate`, `seed` and `burst` are one-shot commands.

Two processes rather than one process with a background thread, because the outage in REQ-OUT is then a real process stop, and the same test proves restart safety (REQ-OPS-01).
The two processes also scale differently: `api` is stateless and can be replicated, while exactly one stock applier is active at a time (NG-11).
They can be started, stopped, deployed and rolled back independently, because they share no memory, queue or file; the only contract between them is the event payload schema and the table ownership rules.

## Running locally

The system needs Python tooling (`uv`) and PostgreSQL 14 or newer, and nothing else (REQ-OPS-02).
The default `ORDERS_STOCK_DATABASE_URL` works unchanged with either way of providing PostgreSQL.

**Native PostgreSQL.**
Install it with the platform's package manager, then make sure the server is running and its client tools (`psql`, `createdb`, `dropdb`) are on `PATH`.
On macOS, Homebrew's `postgresql@17` is keg-only, so its client tools are not linked onto `PATH`, and installing it does not start the server:

```sh
brew install postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
brew services start postgresql@17
```

The `export` lasts only for the current shell, so repeat it, or add it to the shell profile, in any terminal that later runs the client tools.
On Linux, install the distribution package and make sure its service is running: Debian and Ubuntu start it on install, while other distributions may first need the cluster initialized and the service started, as their package documentation describes.
The default `ORDERS_STOCK_DATABASE_URL` and `ORDERS_STOCK_TEST_DATABASE_URL` authenticate with a password over TCP, so `pg_hba.conf` must allow `scram-sha-256` or `md5` for `host` connections from `127.0.0.1/32` and `::1/128`.
Homebrew, Debian and Ubuntu already accept these connections; where a distribution defaults to `ident` for them, as Fedora and RHEL do, change those lines and reload the server.

With the server running, create the role and databases:

```sh
psql -d postgres -c "CREATE ROLE orders_stock LOGIN CREATEDB PASSWORD 'orders_stock'"
createdb -O orders_stock orders_stock
createdb -O orders_stock orders_stock_test
psql -d orders_stock_test -c "ALTER SCHEMA public OWNER TO orders_stock"
```

On Linux these commands run as the `postgres` operating-system user, for example prefixed with `sudo -u postgres`.
The last command lets the test suite drop and recreate the `public` schema of the test database: PostgreSQL 15 and newer give that schema to the database owner, but PostgreSQL 14 leaves it with the bootstrap superuser.

**Docker Compose, optional.**
`docker compose up -d --wait` starts the `postgres:17` image as service `postgres` on port 5432 with user, password and database `orders_stock`, and an init script, `docker/initdb/create-test-database.sql`, creates `orders_stock_test`.
`ORDERS_STOCK_DB_PORT` publishes it on another host port, for example `ORDERS_STOCK_DB_PORT=5433 docker compose up -d --wait` when another PostgreSQL already uses 5432; the container keeps port 5432, and `ORDERS_STOCK_DATABASE_URL` and `ORDERS_STOCK_TEST_DATABASE_URL` must then name the new port.
The service's health check runs `pg_isready` over TCP, so `--wait` returns only once PostgreSQL accepts connections.
There `orders_stock` is the superuser, so it needs no further setup.
Compose provisions PostgreSQL only; the application always runs natively (NG-15).

**The system.**

```sh
uv sync
uv run orders-stock migrate
uv run orders-stock seed
uv run orders-stock api            # terminal 1
uv run orders-stock stock-worker   # terminal 2
```

**Starting clean.**
`seed` only inserts, so a clean run, such as the recorded demo, starts from a new database.
Stop `api`, `stock-worker` and `consume-feed`, drop and recreate the database, then apply the migrations and seed as above.
With native PostgreSQL, run these as the user that created the databases, with the server running and its client tools on `PATH` as described above:

```sh
dropdb --if-exists orders_stock
createdb -O orders_stock orders_stock
```

With Docker Compose:

```sh
docker compose exec postgres dropdb -U orders_stock --if-exists orders_stock
docker compose exec postgres createdb -U orders_stock orders_stock
```

Event IDs start again at 1 in the new database, so start `consume-feed` with `--from-start`.

## Runtime configuration

Every setting carries the prefix `ORDERS_STOCK_`, so that it never collides with a variable such as `DATABASE_URL` that another project exports in the same shell.

| Variable | Default | Used by |
| --- | --- | --- |
| `ORDERS_STOCK_DATABASE_URL` | `postgresql://orders_stock:orders_stock@localhost:5432/orders_stock` | `api`, `stock-worker`, `migrate`, `seed` |
| `ORDERS_STOCK_DATABASE_POOL_TIMEOUT` | `5` (seconds) | `api`, `stock-worker` |
| `ORDERS_STOCK_API_URL` | `http://127.0.0.1:8000` | `burst`, `consume-feed` |
| `ORDERS_STOCK_WORKER_BATCH_SIZE` | `100` | `stock-worker` |
| `ORDERS_STOCK_WORKER_POLL_INTERVAL` | `0.5` (seconds) | `stock-worker` |
| `ORDERS_STOCK_LOG_LEVEL` | `INFO` | all processes |
| `ORDERS_STOCK_TEST_DATABASE_URL` | `postgresql://orders_stock:orders_stock@localhost:5432/orders_stock_test` | the test suite only |
| `ORDERS_STOCK_DB_PORT` | `5432` | `compose.yaml` and `scripts/start.sh --docker` only: the host port the compose database is published on |

`ORDERS_STOCK_DATABASE_URL` is a libpq connection URI.
Settings are read once at start-up into a frozen dataclass; there is no configuration file.

### Database connections

- Connections come from a `psycopg_pool.ConnectionPool`: `min_size=1`, `max_size=10` for `api` and `max_size=1` for `stock-worker`, `timeout` set from `ORDERS_STOCK_DATABASE_POOL_TIMEOUT` to bound the wait to acquire a connection, and `check=ConnectionPool.check_connection` so that a connection broken by a PostgreSQL restart is replaced before use.
- The pool is opened with `wait=False`, so `api` starts even when PostgreSQL is down (REQ-API-03).
- Every connection is configured with autocommit off, isolation level `READ COMMITTED` set explicitly rather than inherited from server defaults, `TimeZone=UTC`, `statement_timeout=5s`, and `application_name` set to `orders-stock-api` or `orders-stock-stock-worker`.
- `seed` opens one direct connection with the same configuration and `application_name` `orders-stock-seed`; `migrate` connects through yoyo.
- Errors of class `psycopg.OperationalError`, which includes connection failures, pool acquisition timeouts and statement timeouts, are mapped to `503 service_unavailable` in `api` and to a retry with backoff in `stock-worker`.

## Stock worker loop

```text
until stop is requested:
    try:
        on the first successful connection:
            log  "stock-worker started: offset=<offset> head=<head> lag=<head - offset>"
        batch = apply_next_batch(conn, batch_size)
    except OperationalError or StockInvariantViolation as error:
        log  the error (StockInvariantViolation as an error, otherwise a warning)
        wait backoff (0.5 s, doubling, capped at 10 s); continue
    reset backoff
    if batch is None:
        wait poll_interval (interruptible by stop); continue
    log  "applied events <first>..<last>: orders=<n> skus=<k> offset=<last> lag=<head - last>"
on stop:
    log  "stock-worker stopped: offset=<offset>"   (offset=unknown if it never connected)
```

The start line waits for PostgreSQL, so when it is down at start-up the retry warnings come first.

`SIGINT` and `SIGTERM` set the stop event.
The loop checks it between transactions, so a graceful stop never interrupts a batch; an ungraceful stop (`SIGKILL`) is covered by transaction atomicity.
Any other exception is a defect: it is logged with its traceback and ends the process with exit status 1, which is safe because a restart resumes from the committed offset.
When a batch is applied the loop continues immediately, so catch-up runs at full speed and polling only happens when the applier is idle.

## Command-line interface

One console script, `orders-stock`, built with `argparse`.
Diagnostics go to stderr; command results go to stdout.
Every command exits `0` on success, `1` on a runtime failure, and `2` on a usage error.

### `migrate`

Usage: `orders-stock migrate`.
Applies pending migrations from `migrations/` with yoyo-migrations, holding yoyo's lock.
Prints `applied <n> migration(s)` and exits `0`; a second run prints `applied 0 migration(s)`.

### `api`

Usage: `orders-stock api [--host 127.0.0.1] [--port 8000]`.
Runs Uvicorn with one worker process and access logging on.

### `stock-worker`

Usage: `orders-stock stock-worker`.
Runs the stock worker loop until `SIGINT` or `SIGTERM`.

### `seed`

Usage: `orders-stock seed`.
In one transaction, inserts every demo product and its stock level that does not exist yet.
It only inserts: it never updates, deletes or truncates anything.
Prints the catalogue with current stock:

```text
sku      name              price_cents  on_hand
APL-003  Apples 1kg                349       40
BAN-001  Bananas 1kg               199       50
BRD-004  Sourdough 800g            425       20
MLK-002  Whole milk 1L             115       30
seed: inserted 4 product(s)
```

The demo catalogue is fixed:

| sku | name | price_cents | initial `on_hand` |
| --- | --- | --- | --- |
| `APL-003` | Apples 1kg | 349 | 40 |
| `BAN-001` | Bananas 1kg | 199 | 50 |
| `BRD-004` | Sourdough 800g | 425 | 20 |
| `MLK-002` | Whole milk 1L | 115 | 30 |

A repeatable demo starts from a new database instead, as described in [Running locally](#running-locally).

### `burst`

Usage: `orders-stock burst [--prefix web]`.
Submits the fixed order set below to `POST /orders` over HTTP.
`<p>` is the prefix; references are `<p>-100045` to `<p>-100050`.

| order_ref | customer_id | items | total_cents | identical submissions |
| --- | --- | --- | --- | --- |
| `<p>-100045` | `cust-42` | `BAN-001` x2, `APL-003` x1 | 747 | 3 |
| `<p>-100046` | `cust-7` | `MLK-002` x4 | 460 | 1 |
| `<p>-100047` | `cust-42` | `BRD-004` x1, `BAN-001` x1 | 624 | 2 |
| `<p>-100048` | `cust-13` | `APL-003` x2, `MLK-002` x1 | 813 | 1 |
| `<p>-100049` | `cust-7` | `BAN-001` x3 | 597 | 2 |
| `<p>-100050` | `cust-99` | `BRD-004` x2 | 850 | 1 |

Phase 1 sends all ten identical submissions concurrently from ten threads that start together on a barrier, so duplicates of the same `order_ref` genuinely race.
Phase 2, after phase 1 completes, resubmits `<p>-100047` with `BRD-004` x3 and `BAN-001` x1, which must be rejected as a conflict.
Each request times out after 5 seconds.
A submission that fails with a transport error, a timeout or `503` is retried with the same body up to 5 attempts with exponential backoff from 0.2 seconds; retrying is safe because `order_ref` makes the request idempotent.
Output, one row per `order_ref` in ascending order, then a summary:

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

The `order_ref` column is left-aligned and as wide as the longest reference or its header, whichever is longer; the other columns are right-aligned under their headers, with two spaces between columns, so the table stays aligned for longer prefixes such as `outage`.
Which submission of a duplicated `order_ref` wins the race is not deterministic; the counts are.
Running `burst` again with the same prefix prints `created=0 duplicate=10 conflict=1`.
The command exits `1` if any submission ends in a status other than 201, 200 or 409, fails after its retries, or cannot be sent because its thread failed, for example on a broken or timed-out start barrier.

### `consume-feed`

Usage: `orders-stock consume-feed [--cursor-file .feed-cursor] [--from-start] [--poll-interval 1.0]`.
Polls `GET /order-events?after=<cursor>&limit=100`.
For each `order.accepted` event it writes one line to stdout and flushes it, as `print(..., flush=True)` does, and only then persists the event's ID as the new cursor.
The flush matters when stdout is a pipe or a file, which Python buffers: without it, a crash after saving the cursor would lose lines the cursor has already passed.
Other events follow the [consumer obligations](04-api.md#consumer-obligations).
An event with an unknown `event_type` prints nothing, and its ID is persisted as the cursor.
An `order.accepted` event with an `event_version` above 1 is not skipped: the consumer logs `consume-feed: stopping at event_id=<id>: unsupported event_version=<v>` to stderr and exits with status 1, leaving the cursor before that event.
The cursor file holds one decimal integer and is replaced atomically: write a temporary file in the same directory, `fsync`, then `os.replace`.
`--from-start` ignores the cursor file and starts from 0.
When a page has `has_more: true` it polls again immediately; otherwise it waits `--poll-interval` seconds.
Each request times out after 5 seconds.
Transport errors, timeouts and `5xx` responses are logged to stderr and retried with exponential backoff from 0.5 seconds, capped at 10 seconds.
`SIGINT` or `SIGTERM` stops it after the current event's cursor is persisted, with exit status 0.
On start it logs `consume-feed: starting after event_id=<cursor> (cursor file <path>)` to stderr.
Output line format:

```text
event_id=7 type=order.accepted order_ref=outage-100045 customer_id=cust-42 total_cents=747 items=APL-003x1,BAN-001x2
```

## Flows

### Happy path

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as api (Orders)
    participant DB as PostgreSQL
    participant W as stock-worker
    participant X as consume-feed

    C->>A: POST /orders web-100045
    A->>DB: BEGIN
    A->>DB: SELECT order web-100045 (none)
    A->>DB: SELECT prices for BAN-001, APL-003
    A->>DB: INSERT order ON CONFLICT DO NOTHING (inserted)
    A->>DB: INSERT order_items
    A->>DB: pg_advisory_xact_lock(append lock)
    A->>DB: INSERT order.accepted event (event_id 1)
    A->>DB: COMMIT (releases the lock)
    A-->>C: 201 Created, status accepted

    loop every poll interval
        W->>DB: BEGIN, lock stock-applier offset FOR UPDATE
        W->>DB: SELECT events after offset 0
        W->>DB: UPDATE orders to stock_committed WHERE status is accepted
        W->>DB: UPDATE stock_levels per SKU in SKU order
        W->>DB: UPDATE offset to 1, COMMIT
    end

    X->>A: GET /order-events?after=0
    A->>DB: SELECT events after 0
    A-->>X: 200, event 1, next_after 1
    Note left of X: print and flush line, then persist cursor 1
```

### Unhappy path 1: duplicate submissions

Sequential duplicates take the fast path: the order is found by its key and compared.
Concurrent duplicates are resolved by the unique index; the full argument is in [05-reliability.md](05-reliability.md#duplicate-submissions).

```mermaid
sequenceDiagram
    autonumber
    participant C1 as Client 1
    participant C2 as Client 2
    participant A as api (Orders)
    participant DB as PostgreSQL

    par concurrent submissions
        C1->>A: POST /orders web-100045
    and
        C2->>A: POST /orders web-100045
    end
    A->>DB: T1 SELECT order (none), INSERT order
    A->>DB: T2 SELECT order (none), INSERT order
    Note over DB: T2 waits on the unique index entry held by T1
    A->>DB: T1 INSERT items and event, COMMIT
    A-->>C1: 201 Created
    Note over DB: T2 resumes, ON CONFLICT DO NOTHING returns no row
    A->>DB: T2 SELECT order (sees T1's committed row), compare content
    A-->>C2: 200 OK with the same order (409 if content differs)
```

### Unhappy path 2: stock applier outage and catch-up

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as api
    participant DB as PostgreSQL
    participant W as stock-worker

    Note over W: killed (kill -9)
    C->>A: POST /orders outage-100045 .. outage-100050
    A->>DB: accept, append events 7..12
    A-->>C: 201 Created x6, status accepted
    C->>A: GET /stock/BAN-001
    A-->>C: on_hand 44, as_of_event_id 6 (stale but honest)

    Note over W: started again
    W->>DB: read offset 6 and head 12, lag 6
    W->>DB: one transaction: events 7..12,<br/>orders to stock_committed,<br/>on_hand decremented, offset 12
    C->>A: GET /stock/BAN-001
    A-->>C: on_hand 38, as_of_event_id 12
```

## Evolution paths

These are not built; they show that the boundaries hold when requirements grow.

- **Inventory in its own service and database.**
  The stock applier reads the HTTP feed instead of calling `read_events`; the payload and offset semantics are identical, so only the transport changes.
  `mark_stock_committed` becomes a `stock.committed` event that Orders consumes; the guarded transition is already idempotent, so at-least-once delivery of that event is safe.
  Inventory then keeps its own copy of the SKUs it tracks instead of a foreign key to `products`.
- **Higher intake throughput.**
  `api` replicas are already safe.
  The append lock serializes the last step of each acceptance; past roughly a thousand orders per second, replace it with the snapshot-horizon technique in [ADR-0004](adr/0004-commit-ordered-event-log.md).
- **Lower latency.**
  `LISTEN/NOTIFY` can wake the stock applier and long-polling feed requests early; polling remains the correctness path.
- **Retention.**
  Partition `order_events` by `event_id` range and drop a partition only when every known consumer is past it.
