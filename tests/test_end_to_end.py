"""API -> PostgreSQL -> Redis -> real Celery worker -> mock ERP (over HTTP) -> PostgreSQL."""

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import redis
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_order_dispatcher
from app.core.config import Settings, get_settings
from app.main import app
from app.workers import tasks
from app.workers.celery_app import celery_app
from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder
from mock_erp.schemas import FailureConfig

pytestmark = pytest.mark.integration

TEST_BROKER_DB = 15  # isolated from the development queue (db 0)
CLIENT_TIMEOUT = 0.3
PAYLOAD = {"customer_id": "CLIENT-123", "product_id": "PRODUCT-456", "quantity": 10}


@pytest.fixture
def celery_worker(
    session_factory: sessionmaker[Session],
    erp_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    broker_url = get_settings().celery_broker_url.rsplit("/", 1)[0] + f"/{TEST_BROKER_DB}"
    redis.Redis.from_url(broker_url).flushdb()  # no leftover messages from earlier runs

    monkeypatch.setattr(celery_app.conf, "broker_url", broker_url)
    # The worker runs in this process: point the task at the test DB and test mock ERP,
    # with tiny retry delays so retries really go through Redis but stay fast.
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            erp_base_url=erp_base_url,
            erp_http_timeout_seconds=CLIENT_TIMEOUT,
            erp_max_attempts=3,
            erp_retry_base_delay_seconds=0.05,
            erp_retry_max_delay_seconds=0.2,
        ),
    )
    # Same pool as production config (a `solo` pool here once hid a prefork-only crash).
    with start_worker(
        celery_app, pool=celery_app.conf.worker_pool, concurrency=2, perform_ping_check=False
    ):
        yield


@pytest.fixture
def api(client: TestClient) -> TestClient:
    """The orders API, publishing real Celery messages."""
    app.dependency_overrides[get_order_dispatcher] = lambda: tasks.enqueue_order_processing
    return client


def wait_until(
    api: TestClient,
    status_url: str,
    condition: Callable[[dict[str, Any]], bool],
    timeout: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        order = api.get(status_url).json()
        if condition(order):
            return order
        time.sleep(0.05)
    raise AssertionError(f"condition not met after {timeout}s, last state: {order}")


def has_status(*statuses: str) -> Callable[[dict[str, Any]], bool]:
    return lambda order: order["status"] in statuses


def submit(api: TestClient) -> str:
    started = time.monotonic()
    response = api.post("/orders", json=PAYLOAD)
    assert response.status_code == 202
    assert time.monotonic() - started < 0.5  # never waits for the ERP
    return response.json()["status_url"]


def count_erp_orders(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(ErpOrder)) or 0


@pytest.mark.usefixtures("celery_worker")
def test_order_is_processed_in_the_background(api: TestClient) -> None:
    order = wait_until(api, submit(api), has_status("SUCCESS"))

    assert order["erp_order_id"]
    assert order["attempt_count"] == 1


@pytest.mark.usefixtures("celery_worker")
def test_transient_503_recovers_through_scheduled_retries(
    api: TestClient, erp_failures: FailureInjector, db_session: Session
) -> None:
    erp_failures.configure(FailureConfig(mode="http_503", count=2))

    order = wait_until(api, submit(api), has_status("SUCCESS"))

    assert order["attempt_count"] == 3
    assert order["last_error"] is None
    assert count_erp_orders(db_session) == 1


@pytest.mark.usefixtures("celery_worker")
def test_permanent_400_fails_without_automatic_retry(
    api: TestClient, erp_failures: FailureInjector
) -> None:
    erp_failures.configure(FailureConfig(mode="http_400"))

    status_url = submit(api)
    wait_until(api, status_url, has_status("FAILED"))
    time.sleep(0.5)  # longer than any retry delay: nothing else happens

    order = api.get(status_url).json()
    assert (order["status"], order["attempt_count"]) == ("FAILED", 1)


@pytest.mark.usefixtures("celery_worker")
def test_lost_response_is_reconciled_with_no_duplicate(
    api: TestClient, erp_failures: FailureInjector, db_session: Session
) -> None:
    erp_failures.configure(FailureConfig(mode="timeout_after_create", delay_seconds=0.8, count=1))

    status_url = submit(api)
    wait_until(api, status_url, has_status("UNKNOWN", "SUCCESS"))
    order = wait_until(api, status_url, has_status("SUCCESS"))

    assert order["attempt_count"] == 2
    assert count_erp_orders(db_session) == 1  # exactly one ERP order


@pytest.mark.usefixtures("celery_worker")
def test_manual_retry_after_permanent_failure(
    api: TestClient, erp_failures: FailureInjector, db_session: Session
) -> None:
    erp_failures.configure(FailureConfig(mode="http_400", count=1))
    status_url = submit(api)
    wait_until(api, status_url, has_status("FAILED"))

    # e.g. the ERP-side data was fixed by an operator
    assert api.post(f"{status_url}/retry").status_code == 202
    order = wait_until(api, status_url, has_status("SUCCESS"))

    assert order["attempt_count"] == 2
    assert api.post(f"{status_url}/retry").status_code == 409  # SUCCESS is terminal
    assert count_erp_orders(db_session) == 1
