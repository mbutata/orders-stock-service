# orders-stock-service

An order-intake and stock service in Python and PostgreSQL.

- `POST /orders` accepts an order idempotently: resubmitting the same `order_ref` never counts twice, even when submissions race.
- Stock is decremented asynchronously by a separate worker process; orders keep being accepted while it is down, and stock catches up exactly when it returns.
- `GET /order-events` is a commit-ordered, replayable feed of accepted orders for other systems to consume.
- `GET /orders/{order_ref}` and `GET /stock/{sku}` read orders and stock levels.

## Design

- [SOLUTION.md](SOLUTION.md): the design narrative and its trade-offs.
- [specs/](specs/README.md): the specification the implementation is built against: requirements, domain model and schema, architecture, the HTTP contract with its [OpenAPI 3.1 document](specs/openapi.yaml), reliability and consistency, acceptance scenarios, the demo script, and architecture decision records.

## Running locally

The system needs `uv` and PostgreSQL 14 or newer; Docker is optional and only provisions PostgreSQL.
Step-by-step run instructions arrive with the implementation.
