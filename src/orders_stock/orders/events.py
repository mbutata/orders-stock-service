"""The event log: an append-only, commit-ordered table that is both outbox and public feed."""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from orders_stock.orders.models import ORDER_ACCEPTED, ORDER_ACCEPTED_VERSION, OrderEvent

# The ASCII bytes of "ordevent". Appenders serialize on this transaction-scoped advisory lock,
# which PostgreSQL releases only once the commit is visible, so event_id order is commit order.
APPEND_LOCK_KEY = 0x6F72646576656E74


def append_event(
    conn: psycopg.Connection, order_ref: str, occurred_at: datetime, payload: dict[str, Any]
) -> int:
    """Take the append lock and append an `order.accepted` event; the caller's last statement."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK_KEY,))
    row = conn.execute(
        """
        INSERT INTO order_events (event_type, event_version, order_ref, occurred_at, payload)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING event_id
        """,
        (ORDER_ACCEPTED, ORDER_ACCEPTED_VERSION, order_ref, occurred_at, Jsonb(payload)),
    ).fetchone()
    assert row is not None
    event_id: int = row[0]
    return event_id


def read_events(conn: psycopg.Connection, after: int, limit: int) -> list[OrderEvent]:
    """Up to `limit` events with `event_id > after`, ascending."""
    rows = conn.execute(
        """
        SELECT event_id, event_type, event_version, order_ref, occurred_at, payload
          FROM order_events
         WHERE event_id > %s
         ORDER BY event_id
         LIMIT %s
        """,
        (after, limit),
    ).fetchall()
    return [OrderEvent(*row) for row in rows]


def head_event_id(conn: psycopg.Connection) -> int:
    """The highest event_id in the log, or 0 when it is empty."""
    row = conn.execute("SELECT coalesce(max(event_id), 0) FROM order_events").fetchone()
    assert row is not None
    head: int = row[0]
    return head
