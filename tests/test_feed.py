"""AC-FEED-*: the order-accepted feed and its demonstration consumer."""

import signal
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from orders_stock.orders import NewOrder, NewOrderItem, accept_order, events

from conftest import O1, Command, LiveApi, connect, count, drain, scalar, wait_until

ORDER_46 = NewOrder("web-100046", "cust-7", (NewOrderItem("MLK-002", 4),))


def order(number: int) -> dict[str, Any]:
    return {
        "order_ref": f"web-{number}",
        "customer_id": "cust-42",
        "items": [{"sku": "BAN-001", "qty": 1}],
    }


def accept_orders(client: TestClient, numbers: range) -> None:
    for number in numbers:
        assert client.post("/orders", json=order(number)).status_code == 201


@pytest.mark.parametrize(
    ("query", "event_ids", "next_after", "has_more"),
    [
        ("after=0&limit=2", [1, 2], 2, True),
        ("after=2&limit=2", [3, 4], 4, True),
        ("after=4&limit=2", [5], 5, False),
        ("after=5", [], 5, False),
        ("", [1, 2, 3, 4, 5], 5, False),
    ],
)
def test_ac_feed_01_pagination(
    client: TestClient, query: str, event_ids: list[int], next_after: int, has_more: bool
) -> None:
    accept_orders(client, range(300001, 300006))

    page = client.get(f"/order-events?{query}").json()

    assert [event["event_id"] for event in page["events"]] == event_ids
    assert page["next_after"] == next_after
    assert page["has_more"] is has_more


def test_ac_feed_02_an_event_is_an_immutable_snapshot_of_the_accepted_order(
    client: TestClient, conn: psycopg.Connection
) -> None:
    created = client.post("/orders", json=O1).json()

    first = client.get("/order-events")
    (event,) = first.json()["events"]
    assert event["event_id"] == 1
    assert event["event_type"] == "order.accepted"
    assert event["event_version"] == 1
    assert event["occurred_at"] == created["accepted_at"]
    assert event["data"] == {
        key: value for key, value in created.items() if key not in ("status", "stock_committed_at")
    }

    drain(conn)
    assert client.get("/order-events").content == first.content


def test_ac_feed_03_only_accepted_orders_produce_events(
    client: TestClient, conn: psycopg.Connection
) -> None:
    client.post("/orders", json=O1)

    statuses = [
        client.post("/orders", json=O1).status_code,
        client.post("/orders", json={**O1, "customer_id": "cust-43"}).status_code,
        client.post(
            "/orders",
            json={
                "order_ref": "web-100051",
                "customer_id": "cust-42",
                "items": [{"sku": "BAN-001", "qty": 1}, {"sku": "XYZ-999", "qty": 1}],
            },
        ).status_code,
        client.post("/orders", json={**O1, "items": [{"sku": "BAN-001", "qty": 0}]}).status_code,
    ]

    assert statuses == [200, 409, 422, 422]
    assert count(conn, "order_events") == 1


BLOCKED_ADVISORY_LOCKS = """
    SELECT count(*) FROM pg_locks
     WHERE locktype = 'advisory' AND NOT granted
       AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""


def test_ac_feed_04_events_become_visible_in_commit_order(
    client: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    appended = threading.Event()
    release = threading.Event()
    original = events.append_event

    def append_then_block(
        connection: psycopg.Connection, order_ref: str, occurred_at: datetime, payload: Any
    ) -> int:
        event_id = original(connection, order_ref, occurred_at, payload)
        if order_ref == "web-100045":
            appended.set()
            release.wait(10)  # hold the append lock, uncommitted
        return event_id

    monkeypatch.setattr(events, "append_event", append_then_block)
    errors: list[BaseException] = []

    def accept(request: NewOrder) -> None:
        try:
            with connect() as own:
                own.autocommit = False
                accept_order(own, request)
        except BaseException as error:
            errors.append(error)

    thread_a = threading.Thread(
        target=accept,
        args=(
            NewOrder(
                "web-100045", "cust-42", (NewOrderItem("BAN-001", 2), NewOrderItem("APL-003", 1))
            ),
        ),
    )
    thread_a.start()
    try:
        assert appended.wait(5), "acceptance A never appended its event"
        thread_b = threading.Thread(target=accept, args=(ORDER_46,))
        thread_b.start()

        assert client.get("/order-events", params={"after": 0}).json()["events"] == []
        wait_until(
            lambda: scalar(conn, BLOCKED_ADVISORY_LOCKS) == 1,
            3,
            "acceptance B to wait on the append lock",
        )
    finally:
        release.set()
        thread_a.join(10)
    thread_b.join(10)

    assert not errors
    feed = client.get("/order-events", params={"after": 0}).json()["events"]
    assert [event["data"]["order_ref"] for event in feed] == ["web-100045", "web-100046"]
    assert feed[0]["event_id"] < feed[1]["event_id"]


def test_ac_feed_05_the_event_log_is_append_only(
    client: TestClient, conn: psycopg.Connection
) -> None:
    client.post("/orders", json=O1)

    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("UPDATE order_events SET payload = '{}'")
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM order_events")


@pytest.mark.parametrize(
    ("query", "parameter"),
    [("after=-1", "after"), ("limit=0", "limit"), ("limit=501", "limit"), ("limit=abc", "limit")],
)
def test_ac_feed_06_invalid_feed_parameters(client: TestClient, query: str, parameter: str) -> None:
    response = client.get(f"/order-events?{query}")

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert parameter in [error.get("parameter") for error in problem["errors"]]


def expected_line(event_id: int, number: int) -> str:
    return (
        f"event_id={event_id} type=order.accepted order_ref=web-{number} "
        "customer_id=cust-42 total_cents=199 items=BAN-001x1"
    )


def test_ac_feed_07_the_demonstration_consumer_prints_persists_and_resumes(
    live_api: LiveApi,
    client: TestClient,
    run_command: Callable[..., Command],
    tmp_path: Path,
) -> None:
    accept_orders(client, range(300001, 300004))
    cursor = tmp_path / "cursor"
    env = {"ORDERS_STOCK_API_URL": live_api.url}

    def consume(*extra: str, lines: int) -> list[str]:
        consumer = run_command(
            "consume-feed", "--cursor-file", str(cursor), "--poll-interval", "0.1", *extra, env=env
        )
        consumer.wait_for_stdout_lines(lines)
        consumer.send_signal(signal.SIGINT)
        assert consumer.wait() == 0
        return consumer.stdout

    assert consume(lines=3) == [expected_line(n, 300000 + n) for n in (1, 2, 3)]
    assert cursor.read_text().strip() == "3"

    accept_orders(client, range(300004, 300006))
    assert consume(lines=2) == [expected_line(n, 300000 + n) for n in (4, 5)]

    assert consume("--from-start", lines=5) == [
        expected_line(n, 300000 + n) for n in (1, 2, 3, 4, 5)
    ]
