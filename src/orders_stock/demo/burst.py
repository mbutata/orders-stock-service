"""The `burst` command: submit a fixed order set concurrently, with duplicates and one conflict.

It is a plain HTTP client and sees the system exactly as another team would.
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from orders_stock.config import Settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
MAX_ATTEMPTS = 5
BACKOFF_INITIAL = 0.2
RETRYABLE_STATUSES = {503}
EXPECTED_STATUSES = (201, 200, 409)

# (reference suffix, customer, items, identical submissions in phase 1)
ORDER_SET: tuple[tuple[str, str, tuple[tuple[str, int], ...], int], ...] = (
    ("100045", "cust-42", (("BAN-001", 2), ("APL-003", 1)), 3),
    ("100046", "cust-7", (("MLK-002", 4),), 1),
    ("100047", "cust-42", (("BRD-004", 1), ("BAN-001", 1)), 2),
    ("100048", "cust-13", (("APL-003", 2), ("MLK-002", 1)), 1),
    ("100049", "cust-7", (("BAN-001", 3),), 2),
    ("100050", "cust-99", (("BRD-004", 2),), 1),
)
CONFLICT_SUFFIX = "100047"
CONFLICT_ITEMS: tuple[tuple[str, int], ...] = (("BRD-004", 3), ("BAN-001", 1))


def order_body(
    prefix: str, suffix: str, customer_id: str, items: tuple[tuple[str, int], ...]
) -> dict[str, Any]:
    return {
        "order_ref": f"{prefix}-{suffix}",
        "customer_id": customer_id,
        "items": [{"sku": sku, "qty": qty} for sku, qty in items],
    }


def burst_orders(prefix: str) -> list[dict[str, Any]]:
    """The six distinct orders of the set, one body each."""
    return [order_body(prefix, suffix, customer, items) for suffix, customer, items, _ in ORDER_SET]


@dataclass
class RefTally:
    statuses: Counter[int] = field(default_factory=Counter)
    failed: int = 0
    total_cents: int | None = None

    @property
    def submitted(self) -> int:
        return sum(self.statuses.values()) + self.failed


def submit(client: httpx.Client, body: dict[str, Any]) -> httpx.Response | None:
    """POST one order, retrying transport errors, timeouts and 503 with the same body.

    Retrying is safe because order_ref makes the request idempotent.
    """
    delay = BACKOFF_INITIAL
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post("/orders", json=body)
        except httpx.TransportError as error:
            logger.warning("burst: %s attempt %d failed: %s", body["order_ref"], attempt, error)
        else:
            if response.status_code not in RETRYABLE_STATUSES:
                return response
            logger.warning(
                "burst: %s attempt %d got %d", body["order_ref"], attempt, response.status_code
            )
        if attempt < MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
    return None


def record(
    tallies: dict[str, RefTally],
    lock: threading.Lock,
    body: dict[str, Any],
    response: httpx.Response | None,
) -> None:
    with lock:
        tally = tallies.setdefault(body["order_ref"], RefTally())
        if response is None or response.status_code not in EXPECTED_STATUSES:
            outcome = "no response" if response is None else f"status {response.status_code}"
            logger.error("burst: %s failed: %s", body["order_ref"], outcome)
            tally.failed += 1
            return
        tally.statuses[response.status_code] += 1
        if response.status_code in (200, 201):
            tally.total_cents = response.json()["total_cents"]


def run_burst(settings: Settings, prefix: str) -> int:
    """Submit the order set, print the per-order_ref summary, and return the exit status."""
    phase_one = [
        order_body(prefix, suffix, customer, items)
        for suffix, customer, items, copies in ORDER_SET
        for _ in range(copies)
    ]
    conflict = order_body(prefix, CONFLICT_SUFFIX, "cust-42", CONFLICT_ITEMS)
    tallies: dict[str, RefTally] = {}
    lock = threading.Lock()

    with httpx.Client(
        base_url=settings.api_url,
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_connections=len(phase_one)),
    ) as client:
        # Phase 1: every submission in its own thread, released together so duplicates race.
        barrier = threading.Barrier(len(phase_one))

        def worker(body: dict[str, Any]) -> None:
            barrier.wait()
            record(tallies, lock, body, submit(client, body))

        threads = [threading.Thread(target=worker, args=(body,)) for body in phase_one]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Phase 2: reuse an order_ref for different content, which must be a conflict.
        record(tallies, lock, conflict, submit(client, conflict))

    for line in summary_lines(tallies):
        print(line)
    return 1 if any(tally.failed for tally in tallies.values()) else 0


def summary_lines(tallies: dict[str, RefTally]) -> list[str]:
    refs = sorted(tallies)
    ref_width = max(len("order_ref"), *(len(ref) for ref in refs))
    lines = [f"{'order_ref':<{ref_width}}  submitted  201  200  409  total_cents"]
    for ref in refs:
        tally = tallies[ref]
        total = "" if tally.total_cents is None else str(tally.total_cents)
        lines.append(
            f"{ref:<{ref_width}}  {tally.submitted:>9}  {tally.statuses[201]:>3}  "
            f"{tally.statuses[200]:>3}  {tally.statuses[409]:>3}  {total:>11}"
        )
    values = tallies.values()
    lines.append(
        f"summary: submitted={sum(tally.submitted for tally in values)} "
        f"created={sum(tally.statuses[201] for tally in values)} "
        f"duplicate={sum(tally.statuses[200] for tally in values)} "
        f"conflict={sum(tally.statuses[409] for tally in values)} "
        f"failed={sum(tally.failed for tally in values)}"
    )
    return lines
