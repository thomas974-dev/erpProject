from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder
from mock_erp.schemas import FailureConfig

pytestmark = pytest.mark.integration

PAYLOAD = {
    "external_order_id": "ext-1",
    "customer_id": "CLIENT-123",
    "product_id": "PRODUCT-456",
    "quantity": 10,
}


def count_erp_orders(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(ErpOrder)) or 0


def set_mode(erp_api: TestClient, **config: Any) -> None:
    response = erp_api.put("/admin/failure-mode", json=config)
    assert response.status_code == 200, response.text


# --- normal mode ---------------------------------------------------------------


def test_create_returns_201_with_erp_id(erp_api: TestClient) -> None:
    response = erp_api.post("/erp/orders", json=PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["erp_order_id"]
    assert body["external_order_id"] == "ext-1"


def test_create_is_idempotent_on_external_order_id(
    erp_api: TestClient, db_session: Session
) -> None:
    first = erp_api.post("/erp/orders", json=PAYLOAD)
    second = erp_api.post("/erp/orders", json=PAYLOAD)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["erp_order_id"] == first.json()["erp_order_id"]
    assert count_erp_orders(db_session) == 1


def test_same_external_id_with_different_payload_returns_409(erp_api: TestClient) -> None:
    erp_api.post("/erp/orders", json=PAYLOAD)

    response = erp_api.post("/erp/orders", json={**PAYLOAD, "quantity": 99})

    assert response.status_code == 409


def test_invalid_payload_returns_422(erp_api: TestClient) -> None:
    response = erp_api.post("/erp/orders", json={**PAYLOAD, "quantity": 0})

    assert response.status_code == 422


def test_lookup_by_external_id(erp_api: TestClient) -> None:
    created = erp_api.post("/erp/orders", json=PAYLOAD).json()

    found = erp_api.get("/erp/orders/by-external-id/ext-1")
    missing = erp_api.get("/erp/orders/by-external-id/unknown")

    assert found.status_code == 200
    assert found.json() == created
    assert missing.status_code == 404


# --- failure injection -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_status"), [("http_400", 400), ("http_429", 429), ("http_503", 503)]
)
def test_http_failure_modes_create_nothing(
    erp_api: TestClient, db_session: Session, mode: str, expected_status: int
) -> None:
    set_mode(erp_api, mode=mode)

    response = erp_api.post("/erp/orders", json=PAYLOAD)

    assert response.status_code == expected_status
    assert count_erp_orders(db_session) == 0


@pytest.mark.parametrize("mode", ["http_429", "http_503"])
def test_throttling_modes_send_retry_after(erp_api: TestClient, mode: str) -> None:
    set_mode(erp_api, mode=mode)

    response = erp_api.post("/erp/orders", json=PAYLOAD)

    assert response.headers["Retry-After"] == "1"


def test_timeout_before_create_creates_nothing(erp_api: TestClient, db_session: Session) -> None:
    set_mode(erp_api, mode="timeout_before_create", delay_seconds=0)

    response = erp_api.post("/erp/orders", json=PAYLOAD)

    assert response.status_code == 503
    assert count_erp_orders(db_session) == 0


def test_timeout_after_create_commits_the_order(erp_api: TestClient, db_session: Session) -> None:
    set_mode(erp_api, mode="timeout_after_create", delay_seconds=0)

    erp_api.post("/erp/orders", json=PAYLOAD)

    assert count_erp_orders(db_session) == 1


def test_failure_count_reverts_to_normal(erp_api: TestClient) -> None:
    set_mode(erp_api, mode="http_503", count=2)

    statuses = [erp_api.post("/erp/orders", json=PAYLOAD).status_code for _ in range(3)]

    assert statuses == [503, 503, 201]
    assert erp_api.get("/admin/failure-mode").json()["mode"] == "normal"


def test_reset_restores_default_mode(erp_api: TestClient) -> None:
    set_mode(erp_api, mode="http_400")

    response = erp_api.delete("/admin/failure-mode")

    assert response.json()["mode"] == "normal"
    assert erp_api.post("/erp/orders", json=PAYLOAD).status_code == 201


def test_invalid_failure_mode_is_rejected(erp_api: TestClient) -> None:
    response = erp_api.put("/admin/failure-mode", json={"mode": "explode"})

    assert response.status_code == 422


def test_injector_consume_without_count_keeps_mode() -> None:
    injector = FailureInjector("normal", default_delay_seconds=1.0)
    injector.configure(FailureConfig(mode="http_503"))

    assert [injector.consume()[0] for _ in range(3)] == ["http_503"] * 3


# --- list endpoint (web UI) ----------------------------------------------------


def test_list_is_empty_without_orders(erp_api: TestClient) -> None:
    response = erp_api.get("/erp/orders")

    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_newest_first_and_honours_limit(erp_api: TestClient) -> None:
    for external_id in ("ext-1", "ext-2", "ext-3"):
        erp_api.post("/erp/orders", json={**PAYLOAD, "external_order_id": external_id})

    all_orders = erp_api.get("/erp/orders").json()
    limited = erp_api.get("/erp/orders", params={"limit": 2}).json()

    assert [o["external_order_id"] for o in all_orders] == ["ext-3", "ext-2", "ext-1"]
    assert [o["external_order_id"] for o in limited] == ["ext-3", "ext-2"]


def test_list_rejects_invalid_limit(erp_api: TestClient) -> None:
    assert erp_api.get("/erp/orders", params={"limit": 0}).status_code == 422


def test_cors_allows_only_the_web_ui_origin(erp_api: TestClient) -> None:
    allowed = erp_api.get("/erp/orders", headers={"Origin": "http://127.0.0.1:8000"})
    other = erp_api.get("/erp/orders", headers={"Origin": "http://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert "access-control-allow-origin" not in other.headers
