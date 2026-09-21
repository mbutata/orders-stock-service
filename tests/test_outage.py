"""AC-OUT-*: unhappy path 2, a stock applier outage and catch-up."""

import signal
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from orders_stock.demo.burst import burst_orders
from orders_stock.inventory import AppliedBatch, apply_next_batch, repository

from conftest import (
    O1,
    Command,
    connect,
    drain,
    offset,
    on_hand,
    order_status,
    reset_data,
    scalar,
    wait_until,
)

SKUS = ("APL-003", "BAN-001", "BRD-004", "MLK-002")
OUTAGE_ORDERS: list[dict[str, Any]] = [
    {"order_ref": "web-100046", "customer_id": "cust-7", "items": [{"sku": "MLK-002", "qty": 4}]},
    {
        "order_ref": "web-100047",
        "customer_id": "cust-42",
        "items": [{"sku": "BRD-004", "qty": 1}, {"sku": "BAN-001", "qty": 1}],
    },
    {
        "order_ref": "web-100048",
        "customer_id": "cust-13",
        "items": [{"sku": "APL-003", "qty": 2}, {"sku": "MLK-002", "qty": 1}],
    },
]
OUTAGE_REFS = [order["order_ref"] for order in OUTAGE_ORDERS]


def accept_during_outage(client: TestClient, conn: psycopg.Connection) -> None:
    """The steps of AC-OUT-01: O1 applied as event 1, then three orders with the applier stopped."""
    assert client.post("/orders", json=O1).status_code == 201
    drain(conn)
    for order in OUTAGE_ORDERS:
        assert client.post("/orders", json=order).status_code == 201


def test_ac_out_01_orders_are_accepted_while_the_stock_applier_is_stopped(
    client: TestClient, conn: psycopg.Connection
) -> None:
    accept_during_outage(client, conn)

    for order_ref in OUTAGE_REFS:
        assert client.get(f"/orders/{order_ref}").json()["status"] == "accepted"
    assert client.get("/stock/BAN-001").json() == {
        "sku": "BAN-001",
        "on_hand": 48,
        "as_of_event_id": 1,
    }
    feed = client.get("/order-events", params={"after": 1}).json()
    assert [(event["event_id"], event["data"]["order_ref"]) for event in feed["events"]] == [
        (2, "web-100046"),
        (3, "web-100047"),
        (4, "web-100048"),
    ]


def test_ac_out_02_stock_catches_up_when_the_applier_resumes(
    client: TestClient, conn: psycopg.Connection
) -> None:
    accept_during_outage(client, conn)

    drain(conn)

    for order_ref in OUTAGE_REFS:
        assert order_status(conn, order_ref) == "stock_committed"
    assert {sku: on_hand(conn, sku) for sku in SKUS} == {
        "APL-003": 37,
        "BAN-001": 47,
        "BRD-004": 19,
        "MLK-002": 25,
    }
    assert client.get("/stock/BAN-001").json()["as_of_event_id"] == 4


def test_ac_out_03_the_outage_does_not_change_the_outcome(
    client: TestClient, conn: psycopg.Connection
) -> None:
    # Run A: one order at a time, the applier drained after each.
    for body in burst_orders("web"):
        assert client.post("/orders", json=body).status_code == 201
        drain(conn)
    run_a = {sku: on_hand(conn, sku) for sku in SKUS}

    # Run B: the same orders with the applier stopped, then drained once.
    reset_data(conn, seed=True)
    for body in burst_orders("web"):
        assert client.post("/orders", json=body).status_code == 201
    drain(conn)
    run_b = {sku: on_hand(conn, sku) for sku in SKUS}

    assert run_a == run_b == {"APL-003": 37, "BAN-001": 44, "BRD-004": 17, "MLK-002": 25}


def test_ac_out_04_a_crash_in_the_middle_of_a_batch_leaves_no_partial_effect(
    client: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post("/orders", json=O1).status_code == 201
    assert client.post("/orders", json=OUTAGE_ORDERS[0]).status_code == 201

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash before the offset update")

    monkeypatch.setattr(repository, "advance_offset", crash)
    with pytest.raises(RuntimeError, match="crash before the offset update"):
        apply_next_batch(conn, 100)

    assert (on_hand(conn, "BAN-001"), on_hand(conn, "APL-003"), on_hand(conn, "MLK-002")) == (
        50,
        40,
        30,
    )
    assert order_status(conn, "web-100045") == order_status(conn, "web-100046") == "accepted"
    assert offset(conn) == 0

    monkeypatch.undo()
    drain(conn)
    assert (on_hand(conn, "BAN-001"), on_hand(conn, "APL-003"), on_hand(conn, "MLK-002")) == (
        48,
        39,
        26,
    )
    assert order_status(conn, "web-100045") == order_status(conn, "web-100046") == "stock_committed"
    assert offset(conn) == 2

    conn.execute("UPDATE consumer_offsets SET last_event_id = 0 WHERE consumer = 'stock-applier'")
    batches = drain(conn)
    assert sum(batch.orders_committed for batch in batches) == 0
    assert sum(batch.orders_skipped for batch in batches) == 2
    assert (on_hand(conn, "BAN-001"), on_hand(conn, "APL-003"), on_hand(conn, "MLK-002")) == (
        48,
        39,
        26,
    )
    assert order_status(conn, "web-100045") == order_status(conn, "web-100046") == "stock_committed"


WORKER_SESSIONS = """
    SELECT pid FROM pg_stat_activity
     WHERE datname = current_database()
       AND application_name = 'orders-stock-stock-worker'
"""


def test_ac_out_05_a_killed_stock_worker_process_recovers_without_double_applying(
    client: TestClient, conn: psycopg.Connection, run_command: Callable[..., Command]
) -> None:
    assert client.post("/orders", json=O1).status_code == 201

    with connect() as blocker, blocker.transaction():
        blocker.execute("SELECT 1 FROM stock_levels WHERE sku = 'APL-003' FOR UPDATE")
        worker = run_command("stock-worker")

        def blocked_pid() -> int | None:
            row = conn.execute(WORKER_SESSIONS + " AND wait_event_type = 'Lock'").fetchone()
            return None if row is None else int(row[0])

        wait_until(lambda: blocked_pid() is not None, 4, "the worker to block on APL-003")
        pid = blocked_pid()
        worker.send_signal(signal.SIGKILL)
        worker.wait()
        raise psycopg.Rollback  # the test connection rolls back

    wait_until(
        lambda: conn.execute(WORKER_SESSIONS + " AND pid = %s", (pid,)).fetchone() is None,
        5,
        f"session {pid} to end",
    )
    assert (on_hand(conn, "BAN-001"), on_hand(conn, "APL-003")) == (50, 40)
    assert order_status(conn, "web-100045") == "accepted"
    assert offset(conn) == 0

    worker = run_command("stock-worker")
    wait_until(lambda: offset(conn) == 1, 10, "the new worker to apply event 1")
    worker.send_signal(signal.SIGTERM)

    assert worker.wait() == 0
    assert any("stock-worker stopped: offset=1" in line for line in worker.stderr)
    assert (on_hand(conn, "BAN-001"), on_hand(conn, "APL-003")) == (48, 39)
    assert order_status(conn, "web-100045") == "stock_committed"


def test_ac_out_06_overlapping_appliers_apply_each_order_once(
    client: TestClient, conn: psycopg.Connection
) -> None:
    for number in range(200001, 200051):
        body = {
            "order_ref": f"web-{number}",
            "customer_id": "cust-1",
            "items": [{"sku": "BAN-001", "qty": 1}],
        }
        assert client.post("/orders", json=body).status_code == 201

    results: list[list[AppliedBatch]] = [[], []]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def applier(index: int) -> None:
        try:
            with connect() as own:
                barrier.wait()
                results[index] = drain(own, batch_size=5)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=applier, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors
    assert on_hand(conn, "BAN-001") == 0
    assert scalar(conn, "SELECT count(*) FROM orders WHERE status = 'stock_committed'") == 50
    assert offset(conn) == 50
    batches = results[0] + results[1]
    assert sum(batch.orders_committed for batch in batches) == 50
    assert sum(batch.orders_skipped for batch in batches) == 0
