"""Order intake and order reads (specs/05-reliability.md, "The acceptance transaction")."""

from collections.abc import Collection

import psycopg

from orders_stock import catalog
from orders_stock.orders import events, repository
from orders_stock.orders.models import (
    AcceptResult,
    NewOrder,
    Order,
    OrderItem,
    OrderRefConflict,
    UnknownSkus,
)


def accept_order(conn: psycopg.Connection, request: NewOrder) -> AcceptResult:
    """Accept an order idempotently by `order_ref`, in its own transaction.

    Returns outcome "created" for a new order and "duplicate" for an identical resubmission.
    Raises OrderRefConflict for a resubmission with different content, and UnknownSkus for a
    new order that references SKUs absent from the catalogue.
    """
    with conn.transaction():
        # 1. Retry fast path. An existing order is answered from the stored order alone,
        #    before the catalogue is consulted, so a retry is never repriced.
        existing = repository.find_order(conn, request.order_ref)
        if existing is None:
            # 2. Price the order from the catalogue.
            prices = catalog.get_prices(conn, [item.sku for item in request.items])
            unknown = [
                (index, item.sku)
                for index, item in enumerate(request.items)
                if item.sku not in prices
            ]
            if unknown:
                raise UnknownSkus(unknown)
            items = tuple(
                OrderItem(
                    sku=item.sku,
                    qty=item.qty,
                    unit_price_cents=prices[item.sku],
                    line_total_cents=item.qty * prices[item.sku],
                )
                for item in sorted(request.items, key=lambda item: item.sku)
            )
            total_cents = sum(item.line_total_cents for item in items)

            # 3. Claim the order_ref. The unique index, not step 1, is the guard: a concurrent
            #    submission waits here for the winner and then finds its committed order.
            accepted_at = repository.insert_order(
                conn, request.order_ref, request.customer_id, total_cents
            )
            if accepted_at is not None:
                # 4. Items, then 5 and 6: the append lock and the event, as the last statement.
                repository.insert_items(conn, request.order_ref, items)
                order = Order(
                    order_ref=request.order_ref,
                    customer_id=request.customer_id,
                    status="accepted",
                    items=items,
                    total_cents=total_cents,
                    accepted_at=accepted_at,
                    stock_committed_at=None,
                )
                events.append_event(conn, order.order_ref, accepted_at, order.snapshot())
                return AcceptResult("created", order)

            # Lost the race: under READ COMMITTED this statement sees the winner's order.
            existing = repository.find_order(conn, request.order_ref)
            assert existing is not None, "ON CONFLICT reported an order that does not exist"

    if existing.content() != request.content():
        raise OrderRefConflict(request.order_ref)
    return AcceptResult("duplicate", existing)


def get_order(conn: psycopg.Connection, order_ref: str) -> Order | None:
    """The order with its items in ascending SKU order, or None."""
    return repository.find_order(conn, order_ref)


def mark_stock_committed(conn: psycopg.Connection, order_refs: Collection[str]) -> set[str]:
    """Move the given orders from `accepted` to `stock_committed` inside the caller's transaction.

    Returns the refs that actually moved; an order already `stock_committed` is left alone.
    """
    if not order_refs:
        return set()
    return repository.update_stock_committed(conn, order_refs)
