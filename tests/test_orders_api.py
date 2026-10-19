import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_order_dispatcher
from app.api.health import get_readiness_checks
from app.main import app
from app.models.order import Order, OrderStatus
from app.services.order_service import OrderNotRetryableError, OrderService

pytestmark = pytest.mark.integration

VALID_PAYLOAD = {"customer_id": "CLIENT-123", "product_id": "PRODUCT-456", "quantity": 10}


def create(
    client: TestClient, headers: dict[str, str] | None = None, **overrides: Any
) -> dict[str, Any]:
    response = client.post("/orders", json={**VALID_PAYLOAD, **overrides}, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


# --- health & docs ---------------------------------------------------------


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_database_and_broker_answer(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "broker": "ok"}}


def test_not_ready_when_a_dependency_is_down(client: TestClient) -> None:
    def broker_down() -> None:
        raise ConnectionError("Error 61 connecting to 127.0.0.1:6379")

    app.dependency_overrides[get_readiness_checks] = lambda: {
        "database": lambda: None,
        "broker": broker_down,
    }

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "broker": "unavailable"},
    }
    assert "6379" not in response.text  # internal details stay in the logs


def test_openapi_documents_all_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) >= {
        "/health",
        "/ready",
        "/orders",
        "/orders/{order_id}",
        "/orders/{order_id}/retry",
    }
    assert client.get("/docs").status_code == 200


# --- POST /orders ----------------------------------------------------------


def test_create_order_returns_202_pending(client: TestClient) -> None:
    response = client.post("/orders", json=VALID_PAYLOAD)

    assert response.status_code == 202
    body = response.json()
    order_id = UUID(body["id"])
    assert body["status"] == "PENDING"
    assert body["external_order_id"]
    assert body["status_url"] == f"/orders/{order_id}"
    assert response.headers["Location"] == body["status_url"]


def test_create_order_trims_whitespace(client: TestClient) -> None:
    body = create(client, customer_id="  CLIENT-123  ")

    order = client.get(body["status_url"]).json()
    assert order["customer_id"] == "CLIENT-123"


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID_PAYLOAD, "quantity": 0},
        {**VALID_PAYLOAD, "quantity": -1},
        {**VALID_PAYLOAD, "quantity": 1.5},
        {**VALID_PAYLOAD, "quantity": "10"},
        {**VALID_PAYLOAD, "quantity": True},
        {**VALID_PAYLOAD, "customer_id": ""},
        {**VALID_PAYLOAD, "product_id": "   "},
        {**VALID_PAYLOAD, "unexpected": "field"},
        {"product_id": "PRODUCT-456", "quantity": 10},
        {"customer_id": "CLIENT-123", "product_id": "PRODUCT-456"},
    ],
    ids=[
        "zero-quantity",
        "negative-quantity",
        "float-quantity",
        "string-quantity",
        "bool-quantity",
        "empty-customer",
        "blank-product",
        "extra-field",
        "missing-customer",
        "missing-quantity",
    ],
)
def test_create_order_rejects_invalid_payload(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/orders", json=payload)

    assert response.status_code == 422


# --- GET /orders/{id} ------------------------------------------------------


def test_get_order_returns_all_fields(client: TestClient) -> None:
    created = create(client)

    response = client.get(created["status_url"])

    assert response.status_code == 200
    order = response.json()
    assert order["id"] == created["id"]
    assert order["quantity"] == 10
    assert order["status"] == "PENDING"
    assert order["erp_order_id"] is None
    assert order["attempt_count"] == 0
    assert order["last_error"] is None
    assert order["created_at"] == order["updated_at"]


def test_get_unknown_order_returns_404(client: TestClient) -> None:
    response = client.get(f"/orders/{uuid4()}")

    assert response.status_code == 404


def test_get_order_with_malformed_id_returns_422(client: TestClient) -> None:
    response = client.get("/orders/not-a-uuid")

    assert response.status_code == 422


# --- GET /orders -----------------------------------------------------------


def test_list_orders_newest_first_with_pagination(client: TestClient) -> None:
    ids = [create(client, quantity=q)["id"] for q in (1, 2, 3)]

    page = client.get("/orders", params={"limit": 2, "offset": 0}).json()

    assert [item["id"] for item in page["items"]] == [ids[2], ids[1]]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)

    next_page = client.get("/orders", params={"limit": 2, "offset": 2}).json()
    assert [item["id"] for item in next_page["items"]] == [ids[0]]


def test_list_orders_defaults(client: TestClient) -> None:
    page = client.get("/orders").json()

    assert page == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_orders_filters_by_status(client: TestClient, db_session: Session) -> None:
    pending = create(client)
    failed_id = create(client)["id"]
    db_session.get_one(Order, UUID(failed_id)).status = OrderStatus.FAILED
    db_session.commit()

    page = client.get("/orders", params={"status": "FAILED"}).json()

    assert [item["id"] for item in page["items"]] == [failed_id]
    assert page["total"] == 1
    assert pending["id"] not in {item["id"] for item in page["items"]}


@pytest.mark.parametrize(
    "params", [{"limit": 101}, {"limit": 0}, {"offset": -1}, {"status": "DONE"}]
)
def test_list_orders_rejects_invalid_query(client: TestClient, params: dict[str, Any]) -> None:
    response = client.get("/orders", params=params)

    assert response.status_code == 422


# --- Idempotency-Key -------------------------------------------------------


def count_orders(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(Order)) or 0


def test_same_idempotency_key_and_payload_returns_original_order(
    client: TestClient, db_session: Session
) -> None:
    headers = {"Idempotency-Key": "key-1"}
    first = client.post("/orders", json=VALID_PAYLOAD, headers=headers)
    second = client.post("/orders", json=VALID_PAYLOAD, headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert count_orders(db_session) == 1


def test_idempotency_compares_normalized_payload(client: TestClient) -> None:
    headers = {"Idempotency-Key": "key-1"}
    first = create(client, headers=headers)
    second = create(client, headers=headers, customer_id="  CLIENT-123 ")

    assert second["id"] == first["id"]


def test_same_idempotency_key_with_different_payload_returns_409(
    client: TestClient, db_session: Session
) -> None:
    headers = {"Idempotency-Key": "key-1"}
    create(client, headers=headers)

    response = client.post("/orders", json={**VALID_PAYLOAD, "quantity": 99}, headers=headers)

    assert response.status_code == 409
    assert count_orders(db_session) == 1


def test_different_idempotency_keys_create_different_orders(client: TestClient) -> None:
    first = create(client, headers={"Idempotency-Key": "key-1"})
    second = create(client, headers={"Idempotency-Key": "key-2"})

    assert first["id"] != second["id"]


def test_requests_without_idempotency_key_are_not_deduplicated(
    client: TestClient, db_session: Session
) -> None:
    create(client)
    create(client)

    assert count_orders(db_session) == 2


def test_oversized_idempotency_key_returns_422(client: TestClient) -> None:
    response = client.post("/orders", json=VALID_PAYLOAD, headers={"Idempotency-Key": "k" * 256})

    assert response.status_code == 422


# --- Background processing is scheduled -----------------------------------------


def test_create_order_schedules_processing_once(client: TestClient, dispatched: list[UUID]) -> None:
    created = create(client)

    assert dispatched == [UUID(created["id"])]


def test_replayed_request_does_not_schedule_a_second_task(
    client: TestClient, dispatched: list[UUID]
) -> None:
    headers = {"Idempotency-Key": "key-1"}
    create(client, headers=headers)
    create(client, headers=headers)

    assert len(dispatched) == 1


def test_broker_failure_still_returns_202_and_keeps_the_order(
    client: TestClient, db_session: Session
) -> None:
    def broken_dispatch(order_id: UUID) -> None:
        raise OperationalError("Redis is down")

    app.dependency_overrides[get_order_dispatcher] = lambda: broken_dispatch

    response = client.post("/orders", json=VALID_PAYLOAD)

    assert response.status_code == 202
    assert count_orders(db_session) == 1  # persisted, stays PENDING (documented limitation)


# --- POST /orders/{id}/retry -----------------------------------------------------


def set_status(db_session: Session, order_id: str, status: OrderStatus) -> None:
    db_session.get_one(Order, UUID(order_id)).status = status
    db_session.commit()


@pytest.mark.parametrize("status", [OrderStatus.FAILED, OrderStatus.UNKNOWN])
def test_retry_failed_or_unknown_order(
    client: TestClient, db_session: Session, dispatched: list[UUID], status: OrderStatus
) -> None:
    created = create(client)
    set_status(db_session, created["id"], status)
    dispatched.clear()

    response = client.post(f"/orders/{created['id']}/retry")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["external_order_id"] == created["external_order_id"]  # never regenerated
    assert response.headers["Location"] == created["status_url"]
    assert dispatched == [UUID(created["id"])]


@pytest.mark.parametrize(
    "status", [OrderStatus.SUCCESS, OrderStatus.PENDING, OrderStatus.PROCESSING]
)
def test_retry_is_rejected_for_other_states(
    client: TestClient, db_session: Session, dispatched: list[UUID], status: OrderStatus
) -> None:
    created = create(client)
    set_status(db_session, created["id"], status)
    dispatched.clear()

    response = client.post(f"/orders/{created['id']}/retry")

    assert response.status_code == 409
    assert status in response.json()["detail"]
    assert dispatched == []  # e.g. a SUCCESS order can never be sent twice


def test_retry_unknown_order_returns_404(client: TestClient) -> None:
    assert client.post(f"/orders/{uuid4()}/retry").status_code == 404


def test_double_click_on_retry_schedules_a_single_task(
    client: TestClient, db_session: Session, dispatched: list[UUID]
) -> None:
    created = create(client)
    set_status(db_session, created["id"], OrderStatus.FAILED)
    dispatched.clear()

    first = client.post(f"/orders/{created['id']}/retry")
    second = client.post(f"/orders/{created['id']}/retry")

    assert (first.status_code, second.status_code) == (202, 409)
    assert len(dispatched) == 1


def test_concurrent_retries_have_exactly_one_winner(
    session_factory: sessionmaker[Session], db_session: Session, client: TestClient
) -> None:
    """Two sessions race on the conditional UPDATE; PostgreSQL lets only one through."""
    order_id = UUID(create(client)["id"])
    set_status(db_session, str(order_id), OrderStatus.FAILED)
    barrier = threading.Barrier(2)

    def retry(_: int) -> str:
        with session_factory() as session:
            service = OrderService(session)
            barrier.wait()
            try:
                service.retry_order(order_id)
                return "accepted"
            except OrderNotRetryableError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(retry, range(2)))

    assert results == ["accepted", "rejected"]
