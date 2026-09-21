# ADR-0008: The technology stack

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-OPS-02, REQ-OPS-03, REQ-API-01, REQ-ARCH-01

## Context

Python and PostgreSQL are fixed.
The system must run natively on macOS and Linux, with Docker optional and limited to provisioning PostgreSQL.
The correctness argument in [05-reliability.md](../05-reliability.md) depends on specific SQL statements executed in a specific order.
The implementation is small and must be easy for a reviewer to read.
This record fixes every tool the implementation uses; the implementation follows it exactly.

## Decision drivers

- The SQL that carries the correctness argument must be visible in the code, statement for statement.
- Nothing to install beyond Python tooling and PostgreSQL.
- Mainstream, maintained tools a reviewer already knows.
- Fewest dependencies that do the job.

## Options considered and decisions

Each section is one decision, with the chosen option listed first.

### Runtime

- **Python 3.14 (chosen).**
  The current stable release; every dependency below ships support for it.
- Python 3.13, rejected only because there is no reason to start a new project on the older release.

### Dependency and environment management

- **uv (chosen).**
  One tool for the virtual environment, the lockfile (`uv.lock`), running commands, and installing the Python interpreter itself.
- Poetry, rejected: slower, and it does not install Python.
- pip with pip-tools and venv, rejected: more manual steps for the same result.

### Web framework

- **FastAPI with Uvicorn and Pydantic v2 (chosen).**
  Declarative request validation, and an OpenAPI-aware ecosystem; the generated schema is replaced by the committed contract (REQ-API-01).
- Flask, rejected: no built-in validation or OpenAPI support.
- Django with Django REST Framework, rejected: ORM-centric and heavy for four endpoints.
- Litestar, rejected: capable, but less familiar to most reviewers, for no functional gain here.

### Concurrency model

- **Synchronous code (chosen).**
  Endpoints are plain `def` functions that FastAPI runs in its thread pool; each request is one short transaction with no fan-out I/O.
- asyncio throughout, rejected: async drivers, pools and fixtures add complexity without a workload that benefits.

### Database access

- **psycopg 3 (`psycopg[binary]`) with `psycopg_pool`, and no ORM (chosen).**
  SQL lives in each component's `repository.py`, rows map to dataclasses through psycopg row factories, and every statement in [05-reliability.md](../05-reliability.md) appears in the code as written.
- SQLAlchemy ORM, rejected: its unit of work decides statement order and hides the exact statements that the correctness argument depends on, such as `ON CONFLICT`, `FOR UPDATE` and the advisory lock.
- SQLAlchemy Core, rejected: a query builder adds a layer for about fifteen statements that are specified verbatim.
- psycopg2, rejected: in maintenance mode, and without a pool that checks connections.
- asyncpg, rejected: async-only.

### Migrations

- **yoyo-migrations with plain SQL files and its `postgresql+psycopg` backend (chosen).**
  Migrations are the SQL of [02-domain-model.md](../02-domain-model.md#ddl), applied in a transaction under yoyo's lock.
  Its release cadence is slow, which is acceptable because plain SQL files move to any other tool unchanged.
- Alembic, rejected: it requires SQLAlchemy, its autogenerate needs ORM models the project does not have, and migrations become Python wrappers around SQL.
- dbmate, rejected: a non-Python binary to install separately.
- A hand-written runner, rejected: it would re-implement locking and bookkeeping.

### Tests

- **pytest against a real PostgreSQL (chosen)**, with FastAPI's `TestClient`, and `jsonschema` with PyYAML to validate responses against `specs/openapi.yaml`.
- Testcontainers, rejected: it requires Docker, contrary to REQ-OPS-02.
- pytest-postgresql, rejected: it adds server process management; the suite uses the same PostgreSQL the developer already runs.
- SQLite for tests, rejected: it lacks exactly the features under test.

### Supporting tools

- **httpx** for the demonstration clients and the tests.
- **argparse** for the command-line interface; Click or Typer would be a dependency for six subcommands.
- **Environment variables read into a frozen dataclass** for configuration; pydantic-settings would be a dependency for seven variables.
- **ruff** for linting and formatting, **mypy** in strict mode with the Pydantic plugin for types, and **import-linter** for the dependency rules.
- **PostgreSQL 14 or newer**; `compose.yaml` pins the `postgres:17` image.
- **GitHub Actions** runs the same commands as a developer, against a PostgreSQL 17 service container.

## Decision

| Package | Minimum version | Group |
| --- | --- | --- |
| `fastapi` | 0.141 | runtime |
| `uvicorn` | 0.53 | runtime |
| `psycopg[binary]` | 3.3 | runtime |
| `psycopg-pool` | 3.3 | runtime |
| `yoyo-migrations` | 9.0 | runtime |
| `httpx` | 0.28 | runtime |
| `pyyaml` | 6.0 | runtime |
| `pytest` | 9.1 | dev |
| `jsonschema` | 4.26 | dev |
| `ruff` | 0.16 | dev |
| `mypy` | 2.3 | dev |
| `import-linter` | 2.15 | dev |

`pyproject.toml` declares these lower bounds, `requires-python = ">=3.14"`, the console script `orders-stock = "orders_stock.cli:main"`, and the `uv_build` backend with `module-name = "orders_stock"`; `uv.lock` pins exact versions.
Development dependencies live in the `dev` dependency group.

Every check runs through uv:

| Check | Command |
| --- | --- |
| Install | `uv sync` |
| Tests | `uv run pytest` |
| Lint and format | `uv run ruff check .` and `uv run ruff format --check .` |
| Types | `uv run mypy src tests` |
| Dependency rules | `uv run lint-imports` |

This combination was verified before it was recorded: the packages install together on Python 3.14.7, and yoyo applied the DDL through psycopg 3 against PostgreSQL 14.

## Consequences

- A reviewer can map each statement in the reliability specification to a line of code.
- A developer needs Python tooling (`uv`) and PostgreSQL, nothing else.
- There is no ORM safety net: queries are checked by the acceptance suite against a real database, and `mypy` checks the Python around them.
- Revisit when the schema grows to where hand-written SQL becomes repetitive: SQLAlchemy Core can be introduced per component without touching the migration files.
