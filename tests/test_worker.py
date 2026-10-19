"""OrderProcessor and the Celery task, against the test database and the real mock ERP."""

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.integrations.erp_client import ErpClient, ErpOrder, ErpTransientError
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate
from app.services.order_processor import OrderProcessor, ProcessOutcome, RetryPolicy
from app.services.order_service import OrderService
from app.workers import tasks
from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder as ErpOrderRow
from mock_erp.schemas import FailureConfig

pytestmark = pytest.mark.integration

CLIENT_TIMEOUT = 0.3
SLOW_ERP = 0.8  # > CLIENT_TIMEOUT
POLICY = RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.05)
PAYLOAD = OrderCreate(customer_id="CLIENT-123", product_id="PRODUCT-456", quantity=10)


class SpyErpClient(ErpClient):
    """Real HTTP client that also records which ERP operations were called."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def create_order(self, **kwargs: Any) -> ErpOrder:
        self.calls.append("create")
        return super().create_order(**kwargs)

    def get_order_by_external_id(self, external_order_id: str) -> ErpOrder | None:
        self.calls.append("lookup")
        return super().get_order_by_external_id(external_order_id)


class LookupDownErpClient(SpyErpClient):
    def get_order_by_external_id(self, external_order_id: str) -> ErpOrder | None:
        self.calls.append("lookup")
        raise ErpTransientError("ERP lookup failed (ReadTimeout)")


@pytest.fixture
def erp(erp_base_url: str) -> Iterator[SpyErpClient]:
    with SpyErpClient(erp_base_url, CLIENT_TIMEOUT) as client:
        yield client


@pytest.fixture
def order_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        return OrderService(session).create_order(PAYLOAD).order.id


def process(
    session_factory: sessionmaker[Session],
    erp: ErpClient,
    order_id: uuid.UUID,
    policy: RetryPolicy = POLICY,
) -> ProcessOutcome | None:
    with session_factory() as session:
        return OrderProcessor(session, erp, policy).process(order_id)


def process_until_settled(
    session_factory: sessionmaker[Session], erp: ErpClient, order_id: uuid.UUID
) -> list[ProcessOutcome]:
    """Run attempts the way the Celery task would, while a retry is scheduled."""
    outcomes = []
    while True:
        outcome = process(session_factory, erp, order_id)
        assert outcome is not None
        outcomes.append(outcome)
        if outcome.retry_in is None:
            return outcomes


def load(session_factory: sessionmaker[Session], order_id: uuid.UUID) -> Order:
    with session_factory() as session:
        return session.get_one(Order, order_id)


def count_erp_orders(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(ErpOrderRow)) or 0


def mark_uncertain(session_factory: sessionmaker[Session], order_id: uuid.UUID) -> None:
    """State left by an earlier attempt whose create call may have reached the ERP."""
    with session_factory() as session:
        order = session.get_one(Order, order_id)
        order.status, order.attempt_count, order.needs_reconciliation = (
            OrderStatus.UNKNOWN,
            1,
            True,
        )
        session.commit()


def set_mode(erp_failures: FailureInjector, **config: Any) -> None:
    erp_failures.configure(FailureConfig.model_validate(config))


# --- RetryPolicy (pure) -------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=30.0)
    no_jitter = lambda: 1.0  # noqa: E731

    assert [policy.delay(n, rng=no_jitter) for n in range(1, 7)] == [1, 2, 4, 8, 16, 30]


def test_backoff_uses_full_jitter() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=30.0)

    assert policy.delay(3, rng=lambda: 0.0) == 0.0
    assert policy.delay(3, rng=lambda: 0.5) == 2.0


def test_retry_after_is_honoured_but_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=30.0)

    assert policy.delay(1, retry_after=5, rng=lambda: 0.5) == 5
    assert policy.delay(1, retry_after=600, rng=lambda: 0.5) == 30


# --- happy path & permanent failure ----------------------------------------------------


def test_success_on_first_attempt(
    session_factory: sessionmaker[Session], erp: SpyErpClient, order_id: uuid.UUID
) -> None:
    outcome = process(session_factory, erp, order_id)

    order = load(session_factory, order_id)
    assert outcome == ProcessOutcome(OrderStatus.SUCCESS)
    assert order.status == OrderStatus.SUCCESS
    assert order.erp_order_id
    assert (order.attempt_count, order.last_error) == (1, None)
    assert erp.calls == ["create"]  # no lookup on the very first attempt


def test_permanent_error_fails_without_retry(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_400")

    outcome = process(session_factory, erp, order_id)

    assert outcome == ProcessOutcome(OrderStatus.FAILED, retry_in=None)
    order = load(session_factory, order_id)
    assert order.attempt_count == 1
    assert order.last_error == "ERP returned HTTP 400: Simulated ERP validation error"
    assert erp.calls == ["create"]


# --- transient failures: bounded retries ------------------------------------------------


def test_transient_503_is_retried_and_recovers(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_503", count=2)

    outcomes = process_until_settled(session_factory, erp, order_id)

    assert [o.status for o in outcomes] == [
        OrderStatus.PENDING,
        OrderStatus.PENDING,
        OrderStatus.SUCCESS,
    ]
    assert all(o.retry_in is not None for o in outcomes[:2])
    order = load(session_factory, order_id)
    assert (order.status, order.attempt_count, order.last_error) == (OrderStatus.SUCCESS, 3, None)
    # 503 means "not processed": no reconciliation needed before retrying.
    assert erp.calls == ["create", "create", "create"]
    assert count_erp_orders(session_factory) == 1


def test_transient_failure_exhausts_the_retry_budget(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_503")

    outcomes = process_until_settled(session_factory, erp, order_id)

    assert len(outcomes) == POLICY.max_attempts
    order = load(session_factory, order_id)
    assert order.status == OrderStatus.FAILED
    assert order.attempt_count == 3
    assert order.last_error == "ERP returned HTTP 503: Simulated ERP 503 (gave up after 3 attempts)"


def test_retry_after_header_sets_the_delay(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_429")
    patient = RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=10)

    outcome = process(session_factory, erp, order_id, policy=patient)

    assert outcome == ProcessOutcome(OrderStatus.PENDING, retry_in=1.0)  # mock sends 1 s


# --- uncertain outcomes: reconciliation --------------------------------------------------


def test_lost_response_is_reconciled_without_a_second_create(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    """PRD core scenario: the ERP committed, then the response was lost."""
    set_mode(erp_failures, mode="timeout_after_create", delay_seconds=SLOW_ERP, count=1)

    first, second = process_until_settled(session_factory, erp, order_id)

    assert first.status == OrderStatus.UNKNOWN
    assert first.retry_in is not None
    assert second == ProcessOutcome(OrderStatus.SUCCESS)
    assert load(session_factory, order_id).needs_reconciliation is False
    assert erp.calls == ["create", "lookup"]  # found by lookup: never re-sent
    with session_factory() as session:
        erp_row = session.scalars(select(ErpOrderRow)).one()  # exactly one ERP order
    assert load(session_factory, order_id).erp_order_id == str(erp_row.id)


def test_timeout_before_create_is_reconciled_then_created(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="timeout_before_create", delay_seconds=SLOW_ERP, count=1)

    outcomes = process_until_settled(session_factory, erp, order_id)

    assert [o.status for o in outcomes] == [OrderStatus.UNKNOWN, OrderStatus.SUCCESS]
    assert erp.calls == ["create", "lookup", "create"]  # confirmed absent, then created
    assert count_erp_orders(session_factory) == 1


def test_unavailable_lookup_keeps_unknown_and_never_creates(
    session_factory: sessionmaker[Session], erp_base_url: str, order_id: uuid.UUID
) -> None:
    mark_uncertain(session_factory, order_id)

    with LookupDownErpClient(erp_base_url, CLIENT_TIMEOUT) as erp:
        outcomes = process_until_settled(session_factory, erp, order_id)

    assert {o.status for o in outcomes} == {OrderStatus.UNKNOWN}
    assert "create" not in erp.calls
    order = load(session_factory, order_id)
    assert order.status == OrderStatus.UNKNOWN  # budget exhausted: still not FAILED
    assert order.last_error == "Reconciliation failed: ERP lookup failed (ReadTimeout)"


def test_uncertain_outcome_with_exhausted_budget_stays_unknown(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="timeout_after_create", delay_seconds=SLOW_ERP)
    single_attempt = RetryPolicy(max_attempts=1, base_delay=0, max_delay=0)

    outcome = process(session_factory, erp, order_id, policy=single_attempt)

    assert outcome == ProcessOutcome(OrderStatus.UNKNOWN, retry_in=None)


def test_erp_order_with_different_data_fails(
    session_factory: sessionmaker[Session], erp: SpyErpClient, order_id: uuid.UUID
) -> None:
    mark_uncertain(session_factory, order_id)
    with session_factory() as session:
        order = session.get_one(Order, order_id)
        session.add(
            ErpOrderRow(
                external_order_id=order.external_order_id,
                customer_id=order.customer_id,
                product_id=order.product_id,
                quantity=999,
            )
        )
        session.commit()

    outcome = process(session_factory, erp, order_id)

    assert outcome == ProcessOutcome(OrderStatus.FAILED)
    assert erp.calls == ["lookup"]
    assert "different order" in (load(session_factory, order_id).last_error or "")


# --- robustness & concurrency ---------------------------------------------------------------


def test_unexpected_error_marks_unknown_and_reraises(
    session_factory: sessionmaker[Session], order_id: uuid.UUID
) -> None:
    class ExplodingErp:
        def create_order(self, **_: Any) -> None:
            raise RuntimeError("bug")

    with session_factory() as session, pytest.raises(RuntimeError):
        OrderProcessor(session, ExplodingErp(), POLICY).process(order_id)  # type: ignore[arg-type]

    order = load(session_factory, order_id)
    assert (order.status, order.last_error) == (OrderStatus.UNKNOWN, "Unexpected worker error")
    # The crash happened after the intent was committed: the next attempt will look it up.
    assert order.needs_reconciliation is True


def test_manual_retry_of_unknown_order_reconciles_first(
    session_factory: sessionmaker[Session], erp: SpyErpClient, order_id: uuid.UUID
) -> None:
    mark_uncertain(session_factory, order_id)
    with session_factory() as session:
        OrderService(session).retry_order(order_id)  # UNKNOWN -> PENDING

    outcome = process(session_factory, erp, order_id)

    assert outcome == ProcessOutcome(OrderStatus.SUCCESS)
    assert erp.calls == ["lookup", "create"]  # absent from the ERP, then created
    assert load(session_factory, order_id).needs_reconciliation is False


def test_settled_orders_are_never_reprocessed(
    session_factory: sessionmaker[Session], erp: SpyErpClient, order_id: uuid.UUID
) -> None:
    process(session_factory, erp, order_id)

    assert process(session_factory, erp, order_id) is None  # e.g. a redelivered message
    assert load(session_factory, order_id).attempt_count == 1
    assert erp.calls == ["create"]


def test_unknown_order_id_is_a_no_op(
    session_factory: sessionmaker[Session], erp: SpyErpClient
) -> None:
    assert process(session_factory, erp, uuid.uuid4()) is None
    assert erp.calls == []


def test_simultaneous_workers_send_the_order_once(
    session_factory: sessionmaker[Session], erp_base_url: str, order_id: uuid.UUID
) -> None:
    """Whatever the timing, the conditional UPDATE lets exactly one worker claim."""
    workers = 4
    barrier = threading.Barrier(workers)

    def work(_: int) -> tuple[ProcessOutcome | None, list[str]]:
        with session_factory() as session, SpyErpClient(erp_base_url, CLIENT_TIMEOUT) as erp:
            processor = OrderProcessor(session, erp, POLICY)
            barrier.wait()
            return processor.process(order_id), erp.calls

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, range(workers)))

    outcomes = [outcome for outcome, _ in results]
    assert outcomes.count(None) == workers - 1
    assert sum(calls.count("create") for _, calls in results) == 1
    assert count_erp_orders(session_factory) == 1
    assert load(session_factory, order_id).attempt_count == 1


# --- secrets ----------------------------------------------------------------------------------


def test_credentials_never_reach_logs_or_last_error(
    session_factory: sessionmaker[Session],
    order_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Credentials embedded in the ERP URL; port 1 refuses connections.
    with (
        caplog.at_level(logging.DEBUG),
        ErpClient("http://svc-user:s3cr3t-token@127.0.0.1:1", CLIENT_TIMEOUT) as erp,
    ):
        process_until_settled(session_factory, erp, order_id)

    order = load(session_factory, order_id)
    assert order.status == OrderStatus.FAILED
    output = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "s3cr3t-token" not in output
    assert "s3cr3t-token" not in (order.last_error or "")


def test_finished_events_carry_the_required_fields(
    session_factory: sessionmaker[Session],
    erp: SpyErpClient,
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_mode(erp_failures, mode="http_400")

    with caplog.at_level(logging.INFO, logger="app.services.order_processor"):
        process(session_factory, erp, order_id)

    finished = json.loads(
        JsonFormatter().format(
            next(r for r in caplog.records if getattr(r, "event", None) == "order_finished")
        )
    )
    assert finished["level"] == "WARNING"
    assert finished["order_id"] == str(order_id)
    assert finished["external_order_id"] == str(order_id)
    assert finished["attempt"] == 1
    assert finished["status"] == "FAILED"
    assert finished["error_category"] == "permanent"
    assert finished["error"].startswith("ERP returned HTTP 400")


# --- the Celery task ------------------------------------------------------------------------


@pytest.fixture
def task_env(
    session_factory: sessionmaker[Session], erp_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Run the task synchronously against the test DB; capture rescheduled attempts."""
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            erp_base_url=erp_base_url,
            erp_http_timeout_seconds=CLIENT_TIMEOUT,
            erp_retry_base_delay_seconds=0.01,
            erp_retry_max_delay_seconds=0.05,
        ),
    )
    monkeypatch.setattr(
        tasks.process_order, "apply_async", lambda **kwargs: scheduled.append(kwargs)
    )
    return scheduled


def test_task_processes_the_order(
    session_factory: sessionmaker[Session],
    task_env: list[dict[str, Any]],
    order_id: uuid.UUID,
) -> None:
    tasks.process_order.run(str(order_id))

    assert load(session_factory, order_id).status == OrderStatus.SUCCESS
    assert task_env == []


def test_task_reschedules_itself_on_transient_failure(
    session_factory: sessionmaker[Session],
    task_env: list[dict[str, Any]],
    erp_failures: FailureInjector,
    order_id: uuid.UUID,
) -> None:
    set_mode(erp_failures, mode="http_503", count=1)

    tasks.process_order.run(str(order_id))

    assert load(session_factory, order_id).status == OrderStatus.PENDING
    assert len(task_env) == 1
    assert task_env[0]["args"] == [str(order_id)]
    assert 0 <= task_env[0]["countdown"] <= 0.05
