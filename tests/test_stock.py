"""AC-STK-*: stock levels and the stock applier."""

import psycopg
from fastapi.testclient import TestClient

from orders_stock.demo.burst import burst_orders
from orders_stock.inventory import apply_next_batch

from conftest import O1, drain, offset, on_hand, order_status, scalar

INITIAL = {"APL-003": 40, "BAN-001": 50, "BRD-004": 20, "MLK-002": 30}


def test_ac_stk_01_the_stock_applier_applies_an_accepted_order(
    client: TestClient, conn: psycopg.Connection
) -> None:
    assert client.post("/orders", json=O1).status_code == 201

    batch = apply_next_batch(conn, 100)

    assert batch is not None
    assert (batch.first_event_id, batch.last_event_id) == (1, 1)
    assert (batch.orders_committed, batch.orders_skipped) == (1, 0)
    assert on_hand(conn, "BAN-001") == 48
    assert on_hand(conn, "APL-003") == 39
    assert on_hand(conn, "BRD-004") == 20
    assert on_hand(conn, "MLK-002") == 30
    assert order_status(conn, "web-100045") == "stock_committed"
    assert scalar(
        conn,
        "SELECT stock_committed_at >= accepted_at FROM orders WHERE order_ref = 'web-100045'",
    )
    assert offset(conn) == 1


def test_ac_stk_02_one_batch_aggregates_several_orders(
    client: TestClient, conn: psycopg.Connection
) -> None:
    for order_ref, items in [
        ("web-100045", [("BAN-001", 2), ("APL-003", 1)]),
        ("web-100047", [("BRD-004", 1), ("BAN-001", 1)]),
        ("web-100049", [("BAN-001", 3)]),
    ]:
        body = {
            "order_ref": order_ref,
            "customer_id": "cust-42",
            "items": [{"sku": sku, "qty": qty} for sku, qty in items],
        }
        assert client.post("/orders", json=body).status_code == 201

    batch = apply_next_batch(conn, 100)

    assert batch is not None
    assert batch.orders_committed == 3
    assert on_hand(conn, "BAN-001") == 44
    assert on_hand(conn, "APL-003") == 39
    assert on_hand(conn, "BRD-004") == 19
    for order_ref in ("web-100045", "web-100047", "web-100049"):
        assert order_status(conn, order_ref) == "stock_committed"
    assert offset(conn) == 3


def test_ac_stk_03_insufficient_stock_produces_a_backorder(
    client: TestClient, conn: psycopg.Connection
) -> None:
    conn.execute("UPDATE stock_levels SET on_hand = 2 WHERE sku = 'BRD-004'")

    response = client.post(
        "/orders",
        json={
            "order_ref": "web-100060",
            "customer_id": "cust-7",
            "items": [{"sku": "BRD-004", "qty": 5}],
        },
    )
    assert response.status_code == 201
    drain(conn)

    assert client.get("/stock/BRD-004").json()["on_hand"] == -3
    assert order_status(conn, "web-100060") == "stock_committed"


def test_ac_stk_04_read_the_stock_of_a_sku(client: TestClient, conn: psycopg.Connection) -> None:
    client.post("/orders", json=O1)
    drain(conn)

    response = client.get("/stock/BAN-001")
    assert response.status_code == 200
    assert response.json() == {"sku": "BAN-001", "on_hand": 48, "as_of_event_id": 1}

    missing = client.get("/stock/XYZ-999")
    assert missing.status_code == 404
    assert missing.json()["code"] == "sku_not_found"


def test_ac_stk_05_stock_is_conserved(client: TestClient, conn: psycopg.Connection) -> None:
    for body in burst_orders("web"):
        assert client.post("/orders", json=body).status_code == 201
    drain(conn)

    committed: dict[str, int] = dict(
        conn.execute(
            """
            SELECT i.sku, sum(i.qty)
              FROM order_items i JOIN orders o USING (order_ref)
             WHERE o.status = 'stock_committed'
             GROUP BY i.sku
            """
        ).fetchall()
    )
    for sku, initial in INITIAL.items():
        assert on_hand(conn, sku) == initial - committed.get(sku, 0)
    assert {sku: on_hand(conn, sku) for sku in INITIAL} == {
        "APL-003": 37,
        "BAN-001": 44,
        "BRD-004": 17,
        "MLK-002": 25,
    }
    mismatched = scalar(
        conn,
        """
        SELECT count(*) FROM orders o
         WHERE o.total_cents <> (SELECT sum(line_total_cents) FROM order_items i
                                  WHERE i.order_ref = o.order_ref)
        """,
    )
    assert mismatched == 0
