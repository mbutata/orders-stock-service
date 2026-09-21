# Specifications

This directory is the design of the orders-stock service.
It was written before the code, and the code is built to conform to it.
A change in behaviour starts here: first the requirement, then the acceptance scenario, then the code.

## Reading order

| Document | Answers |
| --- | --- |
| [01-requirements.md](01-requirements.md) | What must the system do, which failures must it handle, and what does it deliberately not do? |
| [02-domain-model.md](02-domain-model.md) | What are the entities, their invariants and the order lifecycle, and what is the PostgreSQL schema? |
| [03-architecture.md](03-architecture.md) | What are the components and processes, how do they communicate, and how does the system run locally? |
| [04-api.md](04-api.md) and [openapi.yaml](openapi.yaml) | What is the HTTP contract, including idempotency and the feed consumer contract? |
| [05-reliability.md](05-reliability.md) | Why does the system stay correct under duplicates, concurrency, outages and crashes? |
| [06-acceptance.md](06-acceptance.md) | How is each requirement verified? |
| [07-demo.md](07-demo.md) | What does the recorded demonstration run, and what must it show? |
| [adr/](adr/README.md) | Why were the contested choices made, and what was rejected? |

With ten minutes, read [SOLUTION.md](../SOLUTION.md), then [05-reliability.md](05-reliability.md), then the [decision index](adr/README.md).

## Traceability

| ID | Defined in | Meaning |
| --- | --- | --- |
| `REQ-<AREA>-nn` | [01-requirements.md](01-requirements.md) | A testable requirement. |
| `NG-nn` | [01-requirements.md](01-requirements.md#non-goals) | A deliberate non-goal. |
| `INV-<ENTITY>-n` | [02-domain-model.md](02-domain-model.md#entities-and-invariants) | A domain invariant. |
| `AC-<AREA>-nn` | [06-acceptance.md](06-acceptance.md) | An acceptance scenario; its test is named after it. |
| `D-nn` | [07-demo.md](07-demo.md) | A step of the demonstration. |
| `ADR-nnnn` | [adr/](adr/README.md) | A decision record. |

Every requirement names the scenarios or demo steps that verify it, and every scenario names the requirements it verifies.
The matrix in [06-acceptance.md](06-acceptance.md#traceability-matrix) is the reverse index.

## Normative artefacts

Four artefacts are specified exactly and must be matched by the implementation:

- the DDL in [02-domain-model.md](02-domain-model.md#ddl), which becomes migration `0001_initial.sql`;
- [openapi.yaml](openapi.yaml), which the service serves as its own contract and which the tests validate responses against;
- the statement sequences of the acceptance and stock application transactions in [05-reliability.md](05-reliability.md);
- the command-line interface and its output formats in [03-architecture.md](03-architecture.md#command-line-interface).

A discovered mismatch between the specification and the code is a defect in one of them; it is resolved, never left standing.

## Conventions

- The key words MUST, MUST NOT, SHOULD and MAY follow RFC 2119 and RFC 8174.
- Diagrams are Mermaid, so they render on GitHub and diff as text.
- Markdown files put each sentence on its own line, so reviews and diffs point at single statements.

## Status

Version 1.0, accepted on 2026-09-21.
