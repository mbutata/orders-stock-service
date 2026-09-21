"""SQL for stock levels and the stock applier's offset."""

from collections.abc import Mapping
from dataclasses import dataclass

import psycopg

STOCK_APPLIER = "stock-applier"


@dataclass(frozen=True)
class StockLevel:
    sku: str
    on_hand: int
    as_of_event_id: int  # the stock applier's offset when on_hand was read

    def to_json(self) -> dict[str, str | int]:
        return {"sku": self.sku, "on_hand": self.on_hand, "as_of_event_id": self.as_of_event_id}


def get_stock(conn: psycopg.Connection, sku: str) -> StockLevel | None:
    """`on_hand` and the offset, read in one statement so they describe the same moment."""
    row = conn.execute(
        """
        SELECT s.sku, s.on_hand, o.last_event_id
          FROM stock_levels s
         CROSS JOIN consumer_offsets o
         WHERE s.sku = %s AND o.consumer = %s
        """,
        (sku, STOCK_APPLIER),
    ).fetchone()
    return None if row is None else StockLevel(*row)


def insert_missing_stock_levels(conn: psycopg.Connection, levels: Mapping[str, int]) -> int:
    """Insert a stock level for each SKU that has none; return how many were inserted."""
    cursor = conn.execute(
        """
        INSERT INTO stock_levels (sku, on_hand)
        SELECT * FROM unnest(%s::text[], %s::integer[])
        ON CONFLICT (sku) DO NOTHING
        """,
        (list(levels.keys()), list(levels.values())),
    )
    return cursor.rowcount


def lock_offset(conn: psycopg.Connection) -> int:
    """Become the only active applier: lock the offset row and read it."""
    row = conn.execute(
        "SELECT last_event_id FROM consumer_offsets WHERE consumer = %s FOR UPDATE",
        (STOCK_APPLIER,),
    ).fetchone()
    assert row is not None, "the stock-applier offset row is created by the initial migration"
    offset: int = row[0]
    return offset


def read_offset(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT last_event_id FROM consumer_offsets WHERE consumer = %s", (STOCK_APPLIER,)
    ).fetchone()
    assert row is not None, "the stock-applier offset row is created by the initial migration"
    offset: int = row[0]
    return offset


def decrement_stock(conn: psycopg.Connection, sku: str, qty: int) -> int | None:
    """Decrement unconditionally, even below zero; None when the SKU has no stock level."""
    row = conn.execute(
        """
        UPDATE stock_levels
           SET on_hand = on_hand - %s, updated_at = now()
         WHERE sku = %s
        RETURNING on_hand
        """,
        (qty, sku),
    ).fetchone()
    return None if row is None else int(row[0])


def advance_offset(conn: psycopg.Connection, last_event_id: int) -> None:
    conn.execute(
        """
        UPDATE consumer_offsets
           SET last_event_id = %s, updated_at = now()
         WHERE consumer = %s
        """,
        (last_event_id, STOCK_APPLIER),
    )
