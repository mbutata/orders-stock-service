"""The stock applier: turns `order.accepted` events into stock decrements, exactly once.

See specs/05-reliability.md, "The stock application transaction", and the worker loop in
specs/03-architecture.md.
"""

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass

import psycopg

from orders_stock import db, orders
from orders_stock.config import Settings
from orders_stock.inventory import repository

logger = logging.getLogger(__name__)

APPLICATION_NAME = "orders-stock-stock-worker"
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 10.0


class StockInvariantViolation(Exception):
    """An event cannot be applied because an invariant was broken outside the application."""


@dataclass(frozen=True)
class AppliedBatch:
    first_event_id: int
    last_event_id: int
    orders_committed: int
    orders_skipped: int
    skus_touched: int


def apply_next_batch(conn: psycopg.Connection, batch_size: int) -> AppliedBatch | None:
    """Apply the next batch of events in one transaction; None when there is nothing to apply.

    The status transitions, the stock decrements and the offset advance commit together, and
    the guarded transition ignores orders already applied, so each order counts exactly once.
    """
    backorders: list[tuple[str, int]] = []
    with conn.transaction():
        # 1. Become the only active applier and read the offset.
        offset = repository.lock_offset(conn)

        # 2. Read the next batch.
        batch = orders.read_events(conn, offset, batch_size)
        if not batch:
            return None

        accepted = []
        for event in batch:
            if event.event_type != orders.ORDER_ACCEPTED:
                continue  # not ours to handle; the offset still moves past it
            if event.event_version > orders.ORDER_ACCEPTED_VERSION:
                raise StockInvariantViolation(
                    f"event_id={event.event_id} has unsupported event_version={event.event_version}"
                )
            accepted.append(event)

        # 3. Transition the batch's orders; only those still `accepted` come back.
        committed = orders.mark_stock_committed(conn, [event.order_ref for event in accepted])

        # 4. Sum quantities per SKU over the transitioned orders, then decrement in SKU order.
        quantities: defaultdict[str, int] = defaultdict(int)
        first_event_with_sku: dict[str, int] = {}
        skipped = 0
        for event in accepted:
            if event.order_ref not in committed:
                skipped += 1
                logger.warning(
                    "skipped event_id=%d: order %s is already stock_committed",
                    event.event_id,
                    event.order_ref,
                )
                continue
            for item in event.payload["items"]:
                quantities[item["sku"]] += item["qty"]
                first_event_with_sku.setdefault(item["sku"], event.event_id)
        for sku in sorted(quantities):
            on_hand = repository.decrement_stock(conn, sku, quantities[sku])
            if on_hand is None:
                raise StockInvariantViolation(
                    f"event_id={first_event_with_sku[sku]} names SKU {sku!r}, "
                    "which has no stock level"
                )
            if on_hand < 0:
                backorders.append((sku, on_hand))

        # 5. Advance the offset to the last event of the batch.
        repository.advance_offset(conn, batch[-1].event_id)

    for sku, on_hand in backorders:
        logger.warning("backorder: %s on_hand=%d", sku, on_hand)
    return AppliedBatch(
        first_event_id=batch[0].event_id,
        last_event_id=batch[-1].event_id,
        orders_committed=len(committed),
        orders_skipped=skipped,
        skus_touched=len(quantities),
    )


def run_stock_worker(settings: Settings, stop: threading.Event) -> None:
    """Apply batches until `stop` is set, polling when idle and backing off on failures."""
    pool = db.create_pool(
        settings.database_url,
        APPLICATION_NAME,
        max_size=1,
        timeout=settings.database_pool_timeout,
    )
    offset: int | None = None
    head = 0
    backoff = BACKOFF_INITIAL
    try:
        while not stop.is_set():
            try:
                with pool.connection() as conn:
                    if offset is None:
                        offset = repository.read_offset(conn)
                        head = orders.head_event_id(conn)
                        conn.commit()
                        logger.info(
                            "stock-worker started: offset=%d head=%d lag=%d",
                            offset,
                            head,
                            head - offset,
                        )
                    batch = apply_next_batch(conn, settings.stock_worker_batch_size)
                    if batch is not None:
                        head = orders.head_event_id(conn)
            except (psycopg.OperationalError, StockInvariantViolation) as error:
                level = (
                    logging.ERROR if isinstance(error, StockInvariantViolation) else logging.WARNING
                )
                logger.log(level, "stock application failed, retrying in %.1fs: %s", backoff, error)
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            backoff = BACKOFF_INITIAL
            if batch is None:
                stop.wait(settings.stock_worker_poll_interval)
                continue
            offset = batch.last_event_id
            logger.info(
                "applied events %d..%d: orders=%d skus=%d offset=%d lag=%d",
                batch.first_event_id,
                batch.last_event_id,
                batch.orders_committed,
                batch.skus_touched,
                offset,
                head - offset,
            )
    finally:
        pool.close()
    logger.info("stock-worker stopped: offset=%s", "unknown" if offset is None else offset)
