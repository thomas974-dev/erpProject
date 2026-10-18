"""OrderProcessor and the Celery task, against the test database and the real mock ERP."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.erp_client import ErpClient
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate
from app.services.order_processor import OrderProcessor
from app.services.order_service import OrderService
from app.workers import tasks
from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder
from mock_erp.schemas import FailureConfig

pytestmark = pytest.mark.integration

CLIENT_TIMEOUT = 0.3
PAYLOAD = OrderCreate(customer_id="CLIENT-123", product_id="PRODUCT-456", quantity=10)


@pytest.fixture
def erp_client(erp_base_url: str) -> Iterator[ErpClient]:
    with ErpClient(erp_base_url, CLIENT_TIMEOUT) as client:
        yield client


@pytest.fixture
def new_order(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        return OrderService(session).create_order(PAYLOAD).order.id


def process(
    session_factory: sessionmaker[Session], erp_client: ErpClient, order_id: uuid.UUID
) -> OrderStatus | None:
    with session_factory() as session:
        return OrderProcessor(session, erp_client).process(order_id)


def load(session_factory: sessionmaker[Session], order_id: uuid.UUID) -> Order:
    with session_factory() as session:
        return session.get_one(Order, order_id)


def count_erp_orders(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(ErpOrder)) or 0


def set_mode(erp_failures: FailureInjector, **config: Any) -> None:
    erp_failures.configure(FailureConfig.model_validate(config))


# --- outcomes -------------------------------------------------------------------


def test_success_records_erp_order_id(
    session_factory: sessionmaker[Session], erp_client: ErpClient, new_order: uuid.UUID
) -> None:
    status = process(session_factory, erp_client, new_order)

    order = load(session_factory, new_order)
    assert status == order.status == OrderStatus.SUCCESS
    assert order.erp_order_id
    assert order.attempt_count == 1
    assert order.last_error is None
    assert order.updated_at > order.created_at
    assert count_erp_orders(session_factory) == 1


def test_permanent_error_marks_failed(
    session_factory: sessionmaker[Session],
    erp_client: ErpClient,
    erp_failures: FailureInjector,
    new_order: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_400")

    process(session_factory, erp_client, new_order)

    order = load(session_factory, new_order)
    assert order.status == OrderStatus.FAILED
    assert order.erp_order_id is None
    assert order.last_error == "ERP returned HTTP 400: Simulated ERP validation error"


def test_transient_error_marks_failed_until_retries_exist(
    session_factory: sessionmaker[Session],
    erp_client: ErpClient,
    erp_failures: FailureInjector,
    new_order: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_503")

    process(session_factory, erp_client, new_order)

    assert load(session_factory, new_order).status == OrderStatus.FAILED


def test_lost_response_marks_unknown_not_failed(
    session_factory: sessionmaker[Session],
    erp_client: ErpClient,
    erp_failures: FailureInjector,
    new_order: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="timeout_after_create", delay_seconds=0.8)

    process(session_factory, erp_client, new_order)

    order = load(session_factory, new_order)
    assert order.status == OrderStatus.UNKNOWN
    assert "ReadTimeout" in (order.last_error or "")
    assert count_erp_orders(session_factory) == 1  # it does exist in the ERP


def test_unexpected_error_marks_unknown_and_reraises(
    session_factory: sessionmaker[Session], new_order: uuid.UUID
) -> None:
    class ExplodingErp:
        def create_order(self, **_: Any) -> None:
            raise RuntimeError("bug")

    with session_factory() as session, pytest.raises(RuntimeError):
        OrderProcessor(session, ExplodingErp()).process(new_order)  # type: ignore[arg-type]

    order = load(session_factory, new_order)
    assert order.status == OrderStatus.UNKNOWN
    assert order.last_error == "Unexpected worker error"


# --- claiming ------------------------------------------------------------------------


def test_redelivered_message_is_a_no_op(
    session_factory: sessionmaker[Session], erp_client: ErpClient, new_order: uuid.UUID
) -> None:
    process(session_factory, erp_client, new_order)

    second = process(session_factory, erp_client, new_order)

    assert second is None
    assert load(session_factory, new_order).attempt_count == 1
    assert count_erp_orders(session_factory) == 1


def test_unknown_order_id_is_a_no_op(
    session_factory: sessionmaker[Session], erp_client: ErpClient
) -> None:
    assert process(session_factory, erp_client, uuid.uuid4()) is None
    assert count_erp_orders(session_factory) == 0


def test_only_one_of_two_sessions_can_claim(
    session_factory: sessionmaker[Session], erp_client: ErpClient, new_order: uuid.UUID
) -> None:
    with session_factory() as first, session_factory() as second:
        claimed = OrderProcessor(first, erp_client)._claim(new_order)
        claimed_again = OrderProcessor(second, erp_client)._claim(new_order)

    assert claimed is not None
    assert claimed.status == OrderStatus.PROCESSING
    assert claimed_again is None


# --- the Celery task itself (run synchronously, no broker) ------------------------------


def test_task_processes_the_order(
    session_factory: sessionmaker[Session],
    erp_base_url: str,
    new_order: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: Settings(_env_file=None, erp_base_url=erp_base_url)
    )

    tasks.process_order.apply(args=[str(new_order)]).get()

    assert load(session_factory, new_order).status == OrderStatus.SUCCESS
