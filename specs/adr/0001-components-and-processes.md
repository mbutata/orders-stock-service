# ADR-0001: A modular monolith with three components and two processes

- Status: Accepted
- Date: 2026-09-21
- Requirements: REQ-ORD-06, REQ-OUT-01, REQ-OPS-01, REQ-ARCH-01

## Context

Order intake and stock handling must be components that can run and evolve independently.
Intake must keep accepting orders while stock processing is unavailable, and stock must catch up afterwards.
The system may be one process or two.
It is small: four HTTP operations, one background job, a handful of tables, and a few hours of implementation.
The illustrative product record combines a price, which intake needs, with a stock count, which stock handling owns.

## Decision drivers

- Intake must be unaffected by a stock-processing outage, and the outage should be demonstrable as a real failure rather than a simulated one.
- Restarts and crashes must be provably safe.
- Proportionality: every deployable and every boundary must pay for itself.
- Clear ownership of data, so each component can later move without untangling the others.
- Exactly-once stock application without distributed coordination.

## Options considered

### Option 1: One process with a background thread for stock

- Good, because it is the simplest to run: one command, one log.
- Bad, because pausing a thread simulates an outage; it cannot show that the stock side survives its own crash and restart.
- Bad, because a crash of either part takes down both, which is the opposite of independent failure.
- Bad, because the stateless request path and the singleton worker have different scaling needs yet would scale together.

### Option 2: A modular monolith with two processes sharing one database (chosen)

One codebase with three components, Catalogue, Orders and Inventory, each owning its tables; an `api` process and a `stock-worker` process; one PostgreSQL database.

- Good, because the outage is real: killing the `stock-worker` process is the demonstration, and it proves restart safety in the same act.
- Good, because a shared database lets the stock applier change stock, order status and its offset in one local transaction, which is what makes exactly-once application simple.
- Good, because one codebase, one migration history and one test suite keep the work proportionate.
- Bad, because both processes depend on one schema, so schema changes need coordination; strict table ownership limits the blast radius.
- Bad, because Inventory changes order status through Orders code inside its own transaction, a coupling a fully separate deployment would have to replace with an event.

### Option 3: Two services with separate databases

- Good, because it gives the strongest independence: separate deployables, separate schemas.
- Bad, because order status would need a second outbox and consumer flowing back from Inventory to Orders.
- Bad, because the catalogue would have to be replicated or served across the boundary.
- Bad, because it multiplies infrastructure and failure modes several times over for no requirement that needs it.

### Option 4: Split by technical layer

An API service, a worker service, and a shared models package.

- Good, because it looks like two services.
- Bad, because boundaries follow technology rather than the domain, so every change crosses every layer and the shared models package couples everything.

### Sub-decision: where prices live

- **Products, with price and stock, owned by Inventory.**
  Rejected: intake would depend on the component whose outage it must tolerate, and Orders and Inventory would depend on each other.
- **A separate Catalogue component owning `sku`, `name` and `price_cents`, with Inventory owning stock levels (chosen).**
  Intake depends only on reference data and its own tables, and the dependency graph is acyclic: Inventory depends on Orders, Orders on Catalogue.

## Decision

Option 2, with three components: Catalogue, Orders and Inventory.
Dependencies point from Inventory to Orders to Catalogue and never back, and import-linter enforces the direction (REQ-ARCH-01).
The processes share nothing but committed rows in PostgreSQL.

## Consequences

- Intake has no code path into Inventory, so no Inventory failure can reach it.
- The outage and restart scenarios are tested and demonstrated against real processes (AC-OUT-05, D-07, D-08).
- `api` is stateless and can be replicated; the stock applier is a singleton by design (NG-11).
- Schema changes are reviewed per owning component; a foreign key into another component's table is not allowed, except toward the catalogue.
- Revisit when Inventory needs its own release cadence or datastore: the evolution path in [03-architecture.md](../03-architecture.md#evolution-paths) replaces the shared transaction with the public feed and a `stock.committed` event, without changing intake.
