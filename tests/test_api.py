"""AC-API-*: the HTTP contract as a whole."""

from collections.abc import Callable

import pytest
import yaml
from fastapi.testclient import TestClient

from orders_stock.config import Settings
from orders_stock.inventory import repository as inventory_repository
from orders_stock.orders import events, service

from conftest import O1, free_port, selects_whole_suite
from contract import SPEC_PATH, ContractChecker


def test_ac_api_02_framework_errors_use_the_problem_shape(client: TestClient) -> None:
    missing = client.get("/nope")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "not_found"

    wrong_method = client.delete("/orders")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["code"] == "method_not_allowed"


def test_ac_api_03_an_unreachable_database_yields_503(
    make_client: Callable[[Settings], TestClient],
) -> None:
    unreachable = Settings(
        database_url=f"postgresql://orders_stock:orders_stock@127.0.0.1:{free_port()}/nothing_test",
        database_pool_timeout=1,
    )
    client = make_client(unreachable)  # the app starts

    for response in (
        client.post("/orders", json=O1),
        client.get("/orders/web-100045"),
        client.get("/stock/BAN-001"),
        client.get("/order-events"),
    ):
        assert response.status_code == 503
        assert response.json()["code"] == "service_unavailable"
        assert response.headers["retry-after"] == "1"


def test_ac_api_04_unexpected_errors_yield_500_without_leaking_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom-7f3a")

    monkeypatch.setattr(service, "accept_order", boom)
    monkeypatch.setattr(service, "get_order", boom)
    monkeypatch.setattr(events, "read_events", boom)
    monkeypatch.setattr(inventory_repository, "get_stock", boom)

    for response in (
        client.post("/orders", json=O1),
        client.get("/orders/web-100045"),
        client.get("/stock/BAN-001"),
        client.get("/order-events"),
    ):
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert "boom-7f3a" not in response.text


def test_ac_api_05_the_service_publishes_the_committed_contract(client: TestClient) -> None:
    document = client.get("/openapi.json")
    assert document.status_code == 200
    assert document.json() == yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["content-type"].startswith("text/html")


def test_ac_api_01_every_response_conforms_to_the_committed_contract(
    contract: ContractChecker, request: pytest.FixtureRequest
) -> None:
    # Each response was checked when it was received; this test runs last and adds coverage.
    assert contract.violations == []
    assert contract.observed, "no response was checked against the contract"
    if selects_whole_suite(request.config):
        missing = sorted(contract.declared - contract.observed)
        assert missing == [], f"declared responses never observed: {missing}"
