"""The `consume-feed` command: the reference consumer of `GET /order-events`.

It stands in for another team's system, talks to the API over HTTP only, and follows the
consumer obligations in specs/04-api.md: print one line per event, then persist the cursor.
"""

import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

from orders_stock.config import Settings

logger = logging.getLogger(__name__)

ORDER_ACCEPTED = "order.accepted"
SUPPORTED_VERSION = 1
PAGE_LIMIT = 100
REQUEST_TIMEOUT = 5.0
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 10.0


class UnsupportedEventVersion(Exception):
    pass


def read_cursor(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return 0


def write_cursor(path: Path, event_id: int) -> None:
    """Replace the cursor file atomically: temporary file in the same directory, fsync, rename."""
    directory = path.resolve().parent
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(f"{event_id}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def format_event(event: dict[str, Any]) -> str:
    data = event["data"]
    items = ",".join(f"{item['sku']}x{item['qty']}" for item in data["items"])
    return (
        f"event_id={event['event_id']} type={event['event_type']} "
        f"order_ref={data['order_ref']} customer_id={data['customer_id']} "
        f"total_cents={data['total_cents']} items={items}"
    )


def process(event: dict[str, Any]) -> None:
    """Print an `order.accepted` event; skip unknown types; stop on an unknown version."""
    if event["event_type"] != ORDER_ACCEPTED:
        return
    if event["event_version"] > SUPPORTED_VERSION:
        raise UnsupportedEventVersion(
            f"stopping at event_id={event['event_id']}: "
            f"unsupported event_version={event['event_version']}"
        )
    # Flush before the cursor moves: when stdout is a pipe or file, a crash after saving the
    # cursor would otherwise lose lines the cursor has already passed.
    print(format_event(event), flush=True)


def fetch_page(client: httpx.Client, cursor: int, stop: threading.Event) -> dict[str, Any] | None:
    """One feed page, retrying transport errors, timeouts and 5xx; None once stop is set."""
    delay = BACKOFF_INITIAL
    while not stop.is_set():
        try:
            response = client.get("/order-events", params={"after": cursor, "limit": PAGE_LIMIT})
        except httpx.TransportError as error:
            logger.warning("consume-feed: %s; retrying in %.1fs", error, delay)
        else:
            if response.status_code < 500:
                response.raise_for_status()
                page: dict[str, Any] = response.json()
                return page
            logger.warning("consume-feed: HTTP %d; retrying in %.1fs", response.status_code, delay)
        stop.wait(delay)
        delay = min(delay * 2, BACKOFF_MAX)
    return None


def run_feed_consumer(
    settings: Settings,
    cursor_file: Path,
    *,
    from_start: bool,
    poll_interval: float,
    stop: threading.Event,
) -> int:
    """Poll the feed until stop is set; return the exit status."""
    cursor = 0 if from_start else read_cursor(cursor_file)
    logger.info("consume-feed: starting after event_id=%d (cursor file %s)", cursor, cursor_file)
    with httpx.Client(base_url=settings.api_url, timeout=REQUEST_TIMEOUT) as client:
        while not stop.is_set():
            page = fetch_page(client, cursor, stop)
            if page is None:
                break
            for event in page["events"]:
                try:
                    process(event)
                except UnsupportedEventVersion as error:
                    logger.error("consume-feed: %s", error)
                    return 1
                cursor = event["event_id"]
                write_cursor(cursor_file, cursor)
                if stop.is_set():
                    return 0
            if not page["has_more"]:
                stop.wait(poll_interval)
    sys.stdout.flush()
    return 0
