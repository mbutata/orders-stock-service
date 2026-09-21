"""AC-ORD-*: order intake and order reads."""

import copy
import re
import time
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from orders_stock.orders import events

from conftest import O1, count, on_hand

RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def without_state(order: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in order.items() if key not in ("status", "stock_committed_at")
    }


def assert_nothing_stored(conn: psycopg.Connection) -> None:
    assert (count(conn, "orders"), count(conn, "order_items"), count(conn, "order_events")) == (
        0,
        0,
        0,
    )


def test_ac_ord_01_accept_a_new_order(client: TestClient, conn: psycopg.Connection) -> None:
    response = client.post("/orders", json=O1)

    assert response.status_code == 201
    assert response.headers["location"] == "/orders/web-100045"
    body = response.json()
    assert body["order_ref"] == "web-100045"
    assert body["customer_id"] == "cust-42"
    assert body["status"] == "accepted"
    assert body["total_cents"] == 747
    assert body["stock_committed_at"] is None
    assert body["items"] == [
        {"sku": "APL-003", "qty": 1, "unit_price_cents": 349, "line_total_cents": 349},
        {"sku": "BAN-001", "qty": 2, "unit_price_cents": 199, "line_total_cents": 398},
    ]
    assert RFC3339_UTC.match(body["accepted_at"])

    assert count(conn, "orders") == 1
    assert count(conn, "order_items") == 2
    rows = conn.execute("SELECT event_type, event_version, payload FROM order_events").fetchall()
    assert rows == [("order.accepted", 1, without_state(body))]
    assert on_hand(conn, "BAN-001") == 50
    assert on_hand(conn, "APL-003") == 40


def test_ac_ord_02_fetch_an_order(client: TestClient) -> None:
    created = client.post("/orders", json=O1).json()

    response = client.get("/orders/web-100045")

    assert response.status_code == 200
    assert response.json() == created


def test_ac_ord_03_fetch_an_unknown_order(client: TestClient) -> None:
    response = client.get("/orders/web-999999")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "order_not_found"
    assert problem["status"] == 404
    assert problem["type"].endswith("04-api.md#order_not_found")


def test_ac_ord_04_prices_are_fixed_at_acceptance(
    client: TestClient, conn: psycopg.Connection
) -> None:
    client.post("/orders", json=O1)
    conn.execute("UPDATE products SET price_cents = 249 WHERE sku = 'BAN-001'")

    later = client.post(
        "/orders",
        json={
            "order_ref": "web-100046",
            "customer_id": "cust-7",
            "items": [{"sku": "BAN-001", "qty": 1}],
        },
    ).json()

    assert later["items"][0]["unit_price_cents"] == 249
    assert later["total_cents"] == 249
    original = client.get("/orders/web-100045").json()
    assert {item["sku"]: item["unit_price_cents"] for item in original["items"]}["BAN-001"] == 199
    assert original["total_cents"] == 747


def modified(change: Any) -> dict[str, Any]:
    body = copy.deepcopy(O1)
    change(body)
    return body


MALFORMED: list[tuple[str, Any, str]] = [
    ("missing order_ref", modified(lambda b: b.pop("order_ref")), "#/order_ref"),
    ("missing customer_id", modified(lambda b: b.pop("customer_id")), "#/customer_id"),
    ("order_ref with a space", modified(lambda b: b.update(order_ref="web 100045")), "#/order_ref"),
    ("order_ref too long", modified(lambda b: b.update(order_ref="a" * 65)), "#/order_ref"),
    ("no items", modified(lambda b: b.update(items=[])), "#/items"),
    ("zero quantity", modified(lambda b: b["items"][0].update(qty=0)), "#/items/0/qty"),
    ("quantity too large", modified(lambda b: b["items"][0].update(qty=10001)), "#/items/0/qty"),
    ("quantity as a string", modified(lambda b: b["items"][0].update(qty="2")), "#/items/0/qty"),
    ("fractional quantity", modified(lambda b: b["items"][0].update(qty=2.5)), "#/items/0/qty"),
    (
        "repeated SKU",
        modified(
            lambda b: b.update(items=[{"sku": "BAN-001", "qty": 1}, {"sku": "BAN-001", "qty": 2}])
        ),
        "#/items/1/sku",
    ),
    ("unknown member", modified(lambda b: b["items"][0].update(quantity=2)), "#/items/0/quantity"),
    ("body is not JSON", b'{"order_ref":', "#"),
]


@pytest.mark.parametrize(
    ("body", "pointer"), [case[1:] for case in MALFORMED], ids=[case[0] for case in MALFORMED]
)
def test_ac_ord_05_malformed_requests_are_rejected_without_effect(
    client: TestClient, conn: psycopg.Connection, body: Any, pointer: str
) -> None:
    if isinstance(body, bytes):
        response = client.post(
            "/orders", content=body, headers={"content-type": "application/json"}
        )
    else:
        response = client.post("/orders", json=body)

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert pointer in [error.get("pointer") for error in problem["errors"]]
    assert_nothing_stored(conn)


def test_ac_ord_06_unknown_sku(client: TestClient, conn: psycopg.Connection) -> None:
    response = client.post(
        "/orders",
        json={
            "order_ref": "web-100051",
            "customer_id": "cust-42",
            "items": [{"sku": "BAN-001", "qty": 1}, {"sku": "XYZ-999", "qty": 1}],
        },
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "unknown_sku"
    assert len(problem["errors"]) == 1
    assert problem["errors"][0]["pointer"] == "#/items/1/sku"
    assert "XYZ-999" in problem["errors"][0]["detail"]
    assert_nothing_stored(conn)


def test_ac_ord_07_the_order_and_its_event_are_atomic(
    client: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_append(*args: object, **kwargs: object) -> int:
        raise RuntimeError("event append failed")

    monkeypatch.setattr(events, "append_event", failing_append)
    response = client.post("/orders", json=O1)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert_nothing_stored(conn)

    monkeypatch.undo()
    assert client.post("/orders", json=O1).status_code == 201


def test_ac_ord_08_intake_does_not_touch_inventory(
    client: TestClient, conn: psycopg.Connection
) -> None:
    with conn.transaction():
        conn.execute("LOCK TABLE stock_levels, consumer_offsets IN ACCESS EXCLUSIVE MODE")

        started = time.monotonic()
        response = client.post("/orders", json=O1)
        elapsed = time.monotonic() - started

        assert response.status_code == 201
        assert elapsed < 2
        raise psycopg.Rollback  # the locking transaction is then rolled back
