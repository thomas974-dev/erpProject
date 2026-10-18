"""API -> PostgreSQL -> Redis -> real Celery worker -> mock ERP (over HTTP) -> PostgreSQL."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_order_dispatcher
from app.core.config import Settings, get_settings
from app.main import app
from app.workers import tasks
from app.workers.celery_app import celery_app
from mock_erp.failures import FailureInjector
from mock_erp.schemas import FailureConfig

pytestmark = pytest.mark.integration

TEST_BROKER_DB = 15  # isolated from the development queue (db 0)


@pytest.fixture
def celery_worker(
    session_factory: sessionmaker[Session],
    erp_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    broker_url = get_settings().celery_broker_url.rsplit("/", 1)[0] + f"/{TEST_BROKER_DB}"
    redis.Redis.from_url(broker_url).flushdb()  # no leftover messages from earlier runs

    monkeypatch.setattr(celery_app.conf, "broker_url", broker_url)
    # The worker runs in this process: point the task at the test DB and test mock ERP.
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(_env_file=None, erp_base_url=erp_base_url, erp_http_timeout_seconds=0.3),
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


def wait_for_final_status(api: TestClient, status_url: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        order = api.get(status_url).json()
        if order["status"] not in ("PENDING", "PROCESSING"):
            return order
        time.sleep(0.05)
    raise AssertionError(f"order still {order['status']} after {timeout}s")


PAYLOAD = {"customer_id": "CLIENT-123", "product_id": "PRODUCT-456", "quantity": 10}


@pytest.mark.usefixtures("celery_worker")
def test_order_is_processed_in_the_background(api: TestClient) -> None:
    started = time.monotonic()
    response = api.post("/orders", json=PAYLOAD)
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert elapsed < 0.5  # the HTTP request did not wait for the ERP

    order = wait_for_final_status(api, response.json()["status_url"])
    assert order["status"] == "SUCCESS"
    assert order["erp_order_id"]
    assert order["attempt_count"] == 1


@pytest.mark.usefixtures("celery_worker")
def test_slow_erp_does_not_slow_down_the_api(
    api: TestClient, erp_failures: FailureInjector
) -> None:
    erp_failures.configure(FailureConfig(mode="timeout_after_create", delay_seconds=0.8))

    started = time.monotonic()
    response = api.post("/orders", json=PAYLOAD)
    assert time.monotonic() - started < 0.5

    order = wait_for_final_status(api, response.json()["status_url"])
    assert order["status"] == "UNKNOWN"
