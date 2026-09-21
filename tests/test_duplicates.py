"""AC-DUP-*: unhappy path 1, duplicate submissions."""

import copy
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import O1, LiveApi, count, drain, on_hand


def items_of(body: dict[str, Any]) -> set[tuple[str, int]]:
    return {(item["sku"], item["qty"]) for item in body["items"]}


def race(live_api: LiveApi, bodies: list[dict[str, Any]]) -> list[httpx.Response]:
    """Post every body from its own thread, all released together by a barrier."""
    barrier = threading.Barrier(len(bodies))
    client = live_api.client()

    def post(body: dict[str, Any]) -> httpx.Response:
        barrier.wait(timeout=10)  # a broken race raises BrokenBarrierError instead of hanging
        return client.post("/orders", json=body)

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        futures = [pool.submit(post, body) for body in bodies]
        return [future.result() for future in futures]


def test_ac_dup_01_an_identical_resubmission_returns_the_existing_order(
    client: TestClient, conn: psycopg.Connection
) -> None:
    created = client.post("/orders", json=O1).json()

    response = client.post("/orders", json=O1)

    assert response.status_code == 200
    assert response.json() == created
    assert count(conn, "orders") == 1
    assert count(conn, "order_events") == 1
    drain(conn)
    assert on_hand(conn, "BAN-001") == 48
    assert on_hand(conn, "APL-003") == 39


def test_ac_dup_02_item_order_does_not_matter(client: TestClient) -> None:
    client.post("/orders", json=O1)
    reversed_items = {**O1, "items": list(reversed(O1["items"]))}

    assert client.post("/orders", json=reversed_items).status_code == 200


def change_ban_qty(body: dict[str, Any]) -> None:
    body["items"][0]["qty"] = 3


def change_customer(body: dict[str, Any]) -> None:
    body["customer_id"] = "cust-43"


def add_item(body: dict[str, Any]) -> None:
    body["items"].append({"sku": "MLK-002", "qty": 1})


def remove_apples(body: dict[str, Any]) -> None:
    body["items"] = [item for item in body["items"] if item["sku"] != "APL-003"]


@pytest.mark.parametrize(
    "modify",
    [change_ban_qty, change_customer, add_item, remove_apples],
    ids=["BAN-001 quantity 3", "customer cust-43", "additional MLK-002", "APL-003 removed"],
)
def test_ac_dup_03_a_conflicting_resubmission_is_rejected(
    client: TestClient, conn: psycopg.Connection, modify: Any
) -> None:
    created = client.post("/orders", json=O1).json()
    body = copy.deepcopy(O1)
    modify(body)

    response = client.post("/orders", json=body)

    assert response.status_code == 409
    assert response.json()["code"] == "order_ref_conflict"
    assert client.get("/orders/web-100045").json() == created
    assert count(conn, "order_events") == 1


def test_ac_dup_04_concurrent_identical_submissions_create_one_order(
    live_api: LiveApi, conn: psycopg.Connection
) -> None:
    responses = race(live_api, [O1] * 20)

    assert Counter(response.status_code for response in responses) == {201: 1, 200: 19}
    bodies = [response.json() for response in responses]
    assert {body["order_ref"] for body in bodies} == {"web-100045"}
    assert all(body["items"] == bodies[0]["items"] for body in bodies)
    assert {body["total_cents"] for body in bodies} == {747}
    assert count(conn, "orders") == 1
    assert count(conn, "order_events") == 1
    drain(conn)
    assert on_hand(conn, "BAN-001") == 48
    assert on_hand(conn, "APL-003") == 39


def test_ac_dup_05_a_resubmission_after_stock_was_committed(
    client: TestClient, conn: psycopg.Connection
) -> None:
    client.post("/orders", json=O1)
    drain(conn)

    response = client.post("/orders", json=O1)

    assert response.status_code == 200
    assert response.json()["status"] == "stock_committed"
    assert response.json()["stock_committed_at"] is not None
    assert count(conn, "order_events") == 1
    drain(conn)
    assert on_hand(conn, "BAN-001") == 48


def test_ac_dup_06_a_retry_is_never_repriced(client: TestClient, conn: psycopg.Connection) -> None:
    client.post("/orders", json=O1)
    conn.execute("UPDATE products SET price_cents = 249 WHERE sku = 'BAN-001'")

    response = client.post("/orders", json=O1)

    assert response.status_code == 200
    body = response.json()
    assert {item["sku"]: item["unit_price_cents"] for item in body["items"]}["BAN-001"] == 199
    assert body["total_cents"] == 747


def test_ac_dup_07_an_existing_order_ref_is_resolved_before_catalogue_validation(
    client: TestClient,
) -> None:
    client.post("/orders", json=O1)

    response = client.post(
        "/orders",
        json={
            "order_ref": "web-100045",
            "customer_id": "cust-42",
            "items": [{"sku": "XYZ-999", "qty": 1}],
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "order_ref_conflict"


def test_ac_dup_08_concurrent_submissions_with_different_content(
    live_api: LiveApi, client: TestClient, conn: psycopg.Connection
) -> None:
    other = {**O1, "items": [{"sku": "BAN-001", "qty": 3}]}
    bodies = [O1] * 5 + [other] * 5

    responses = race(live_api, bodies)

    created = [
        body
        for body, response in zip(bodies, responses, strict=True)
        if response.status_code == 201
    ]
    assert len(created) == 1
    stored = client.get("/orders/web-100045").json()
    assert items_of(stored) == items_of(created[0])
    for body, response in zip(bodies, responses, strict=True):
        if response.status_code != 201:
            expected = 200 if items_of(body) == items_of(stored) else 409
            assert response.status_code == expected
    assert count(conn, "orders") == 1
    assert count(conn, "order_events") == 1
