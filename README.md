# orders-stock-service

An order-intake and stock service in Python and PostgreSQL.

- `POST /orders` accepts an order idempotently: resubmitting the same `order_ref` never counts twice, even when submissions race.
- Stock is decremented asynchronously by a separate worker process; orders keep being accepted while it is down, and stock catches up exactly when it returns.
- `GET /order-events` is a commit-ordered, replayable feed of accepted orders for other systems to consume.
- `GET /orders/{order_ref}` and `GET /stock/{sku}` read orders and stock levels.

## Design

Start with [SOLUTION.md](SOLUTION.md), a ten-minute read; [specs/](specs/README.md) is the detailed reference.

- [SOLUTION.md](SOLUTION.md): the design narrative, its trade-offs, and implementation notes.
- [specs/](specs/README.md): the specification the implementation is built against: requirements, domain model and schema, architecture, the HTTP contract with its [OpenAPI 3.1 document](specs/openapi.yaml), reliability and consistency, acceptance scenarios, the demo script, and architecture decision records.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which also installs Python 3.14 when it is missing.
- PostgreSQL 14 or newer, installed natively or through Docker Compose.
- `curl`, for `scripts/start.sh` and the demo, and optionally `jq`, which only pretty-prints.

## PostgreSQL

The default connection settings expect PostgreSQL on `localhost:5432` with a role `orders_stock` (password `orders_stock`) owning the databases `orders_stock` and `orders_stock_test`.
Set `ORDERS_STOCK_DATABASE_URL` and `ORDERS_STOCK_TEST_DATABASE_URL` to use anything else; every variable is listed in [Runtime configuration](specs/03-architecture.md#runtime-configuration).

### macOS (Homebrew)

If PostgreSQL 14 or newer is already running, skip the first three lines and run only the role and database commands.

```sh
brew install postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"   # keg-only; repeat in each new shell
brew services start postgresql@17
psql -d postgres -c "CREATE ROLE orders_stock LOGIN CREATEDB PASSWORD 'orders_stock'"
createdb -O orders_stock orders_stock
createdb -O orders_stock orders_stock_test
psql -d orders_stock_test -c "ALTER SCHEMA public OWNER TO orders_stock"
```

### Linux

Install the distribution's PostgreSQL package and make sure the service is running.
Debian and Ubuntu start it on install; other distributions may first need the cluster initialized, as their package documentation describes.
Then run the same role and database commands as the `postgres` user:

```sh
sudo -u postgres psql -c "CREATE ROLE orders_stock LOGIN CREATEDB PASSWORD 'orders_stock'"
sudo -u postgres createdb -O orders_stock orders_stock
sudo -u postgres createdb -O orders_stock orders_stock_test
sudo -u postgres psql -d orders_stock_test -c "ALTER SCHEMA public OWNER TO orders_stock"
```

The connections authenticate with a password over TCP, so `pg_hba.conf` must allow `scram-sha-256` or `md5` for `host` connections from `127.0.0.1/32` and `::1/128`.
Debian and Ubuntu already do; where a distribution defaults to `ident`, as Fedora and RHEL do, change those lines and reload the server.

### Docker Compose (optional)

```sh
docker compose up -d --wait
```

This starts `postgres:17` on port 5432 with both databases already created; nothing else is needed.
`ORDERS_STOCK_DB_PORT` publishes it on another host port instead, as [When port 5432 is in use](#when-port-5432-is-in-use) shows.
Compose provisions PostgreSQL only: the application always runs natively.

## Run

### Quick start

With PostgreSQL set up as above, start everything in one terminal:

```sh
scripts/start.sh            # or: scripts/start.sh --docker, to start PostgreSQL with Docker Compose first
```

It checks the prerequisites and the database, installs dependencies, applies the migrations, seeds the catalogue, and runs `api` and `stock-worker` until Ctrl-C, with their logs prefixed in that terminal and also written to `.run/`.
`scripts/start.sh --fresh` first drops and recreates the local database, for a clean demo take.
Then, in a second terminal, step through the demo:

```sh
scripts/demo.sh
```

It shows each step's commands and runs them when Enter is pressed.

### When port 5432 is in use

If another PostgreSQL already listens on port 5432, publish the compose database on a free port instead:

```sh
ORDERS_STOCK_DB_PORT=5433 scripts/start.sh --docker --fresh
```

`scripts/start.sh --docker` then connects on that port, and `scripts/demo.sh` follows it without further settings.
If that port is taken too, including by a server bound only to `127.0.0.1`, `start.sh` stops and says so; choose another, such as 5434.
To use your own PostgreSQL on another port instead, export both addresses with that port:

```sh
export ORDERS_STOCK_DATABASE_URL=postgresql://orders_stock:orders_stock@localhost:5433/orders_stock
export ORDERS_STOCK_TEST_DATABASE_URL=postgresql://orders_stock:orders_stock@localhost:5433/orders_stock_test
scripts/start.sh --fresh
```

On either route, `uv run pytest` needs `ORDERS_STOCK_TEST_DATABASE_URL` exported with that port in the terminal that runs it.

### Manual steps

The reference for what the scripts do:

```sh
uv sync
uv run orders-stock migrate          # applied 1 migration(s)
uv run orders-stock seed             # the demo catalogue and initial stock
uv run orders-stock api              # terminal 1: http://127.0.0.1:8000, docs at /docs
uv run orders-stock stock-worker     # terminal 2: applies stock from the event log
uv run orders-stock consume-feed     # terminal 3, optional: prints order.accepted events
uv run orders-stock burst            # submits a burst of orders, including duplicates
```

On macOS the first `migrate` can pause for several seconds while the migration tool resolves the host name; it is not stuck.
Every command is described in [Command-line interface](specs/03-architecture.md#command-line-interface).

## Demo

The scripted walkthrough, including the duplicate burst and the stock-worker outage and catch-up, is [specs/07-demo.md](specs/07-demo.md).
It starts from a new database, as [Starting clean](specs/03-architecture.md#running-locally) describes.
`scripts/start.sh --fresh` performs that clean start, and `scripts/demo.sh` runs the walkthrough step by step.

## Checks

```sh
uv sync
uv run pytest                  # the acceptance suite, against ORDERS_STOCK_TEST_DATABASE_URL
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run lint-imports            # the component dependency rules
```

Each test is named after the acceptance scenario it verifies in [specs/06-acceptance.md](specs/06-acceptance.md).
The suite recreates the schema of the test database and refuses to run against a database whose name does not end in `_test`.
CI runs the same commands against PostgreSQL 17 ([.github/workflows/ci.yml](.github/workflows/ci.yml)).
