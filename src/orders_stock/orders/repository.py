"""SQL for orders and order items."""

from collections.abc import Collection, Sequence
from datetime import datetime

import psycopg
from psycopg import sql

from orders_stock.orders.models import Order, OrderItem


def find_order(conn: psycopg.Connection, order_ref: str) -> Order | None:
    """The order with its items in ascending SKU order, or None."""
    row = conn.execute(
        """
        SELECT customer_id, status, total_cents, accepted_at, stock_committed_at
          FROM orders WHERE order_ref = %s
        """,
        (order_ref,),
    ).fetchone()
    if row is None:
        return None
    customer_id, status, total_cents, accepted_at, stock_committed_at = row
    items = conn.execute(
        """
        SELECT sku, qty, unit_price_cents, line_total_cents
          FROM order_items WHERE order_ref = %s
         ORDER BY sku COLLATE "C"
        """,
        (order_ref,),
    ).fetchall()
    return Order(
        order_ref=order_ref,
        customer_id=customer_id,
        status=status,
        items=tuple(OrderItem(*item) for item in items),
        total_cents=total_cents,
        accepted_at=accepted_at,
        stock_committed_at=stock_committed_at,
    )


def insert_order(
    conn: psycopg.Connection, order_ref: str, customer_id: str, total_cents: int
) -> datetime | None:
    """Claim the order_ref; None when a concurrent transaction committed it first."""
    row = conn.execute(
        """
        INSERT INTO orders (order_ref, customer_id, total_cents)
        VALUES (%s, %s, %s)
        ON CONFLICT (order_ref) DO NOTHING
        RETURNING accepted_at
        """,
        (order_ref, customer_id, total_cents),
    ).fetchone()
    return None if row is None else row[0]


def insert_items(conn: psycopg.Connection, order_ref: str, items: Sequence[OrderItem]) -> None:
    """Insert the order's items in one statement, in the order given."""
    values = sql.SQL(", ").join(sql.SQL("(%s, %s, %s, %s)") for _ in items)
    query = sql.SQL(
        "INSERT INTO order_items (order_ref, sku, qty, unit_price_cents) VALUES {}"
    ).format(values)
    params = [
        value for item in items for value in (order_ref, item.sku, item.qty, item.unit_price_cents)
    ]
    conn.execute(query, params)


def update_stock_committed(conn: psycopg.Connection, order_refs: Collection[str]) -> set[str]:
    """The guarded transition: only orders still `accepted` move, and at most once."""
    rows = conn.execute(
        """
        UPDATE orders
           SET status = 'stock_committed', stock_committed_at = statement_timestamp()
         WHERE order_ref = ANY(%s) AND status = 'accepted'
        RETURNING order_ref
        """,
        (list(order_refs),),
    ).fetchall()
    return {order_ref for (order_ref,) in rows}
