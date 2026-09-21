# Architecture decision records

Each record captures one decision that had real alternatives, why the chosen option won, and what it costs.
Records are immutable once accepted: a changed decision gets a new record that supersedes the old one, and the old one's status says so.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-components-and-processes.md) | A modular monolith: three components, two processes, one database | Accepted |
| [0002](0002-postgresql-as-message-substrate.md) | PostgreSQL is the message substrate; no external broker | Accepted |
| [0003](0003-transactional-outbox.md) | Downstream work is recorded with a transactional outbox | Accepted |
| [0004](0004-commit-ordered-event-log.md) | Event IDs follow commit order, enforced by an advisory lock | Accepted |
| [0005](0005-order-ref-idempotency.md) | `order_ref` is the idempotency key, enforced by a unique index | Accepted |
| [0006](0006-asynchronous-stock-decrement.md) | Stock is decremented asynchronously and may go negative | Accepted |
| [0007](0007-integration-surface-pull-feed.md) | The integration surface is an HTTP pull feed over the event log | Accepted |
| [0008](0008-technology-stack.md) | The technology stack | Accepted |

## Format

Every record uses the same sections, adapted from MADR:

- **Header**: status, date, and the requirements the decision serves.
- **Context**: the forces at play, stated without reference to the chosen option.
- **Decision drivers**: the criteria the options are judged by.
- **Options considered**: every serious option, each with the reasons for and against it.
- **Decision**: the chosen option and the reason it wins against the drivers.
- **Consequences**: what becomes easier, what becomes harder, and the conditions under which the decision should be revisited.
