from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.erp_client import (
    ErpClient,
    ErpOrder,
    ErpPermanentError,
    ErpTransientError,
    ErpUncertainError,
    parse_retry_after,
)
from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder as ErpOrderRow
from mock_erp.schemas import FailureConfig

ORDER = {
    "external_order_id": "ext-1",
    "customer_id": "CLIENT-123",
    "product_id": "PRODUCT-456",
    "quantity": 10,
}
ERP_BODY = {**ORDER, "erp_order_id": "erp-1", "created_at": "2026-01-01T00:00:00Z"}

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> ErpClient:
    return ErpClient("http://erp.test", 1.0, transport=httpx.MockTransport(handler))


def respond(status_code: int, json: Any = None, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx.Response(status_code, json=json, headers=headers)


def raise_error(error_type: type[httpx.TransportError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("simulated", request=request)

    return handler


# --- create_order: classification (unit, no network) ---------------------------


@pytest.mark.parametrize("status_code", [200, 201])
def test_create_success(status_code: int) -> None:
    with make_client(respond(status_code, ERP_BODY)) as client:
        result = client.create_order(**ORDER)

    assert result == ErpOrder.model_validate(ERP_BODY)


def test_create_sends_external_order_id() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json=ERP_BODY)

    with make_client(handler) as client:
        client.create_order(**ORDER)

    assert sent[0].method == "POST"
    assert sent[0].url.path == "/erp/orders"
    assert b'"external_order_id":"ext-1"' in sent[0].content


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (respond(400, {"detail": "bad product"}), ErpPermanentError),
        (respond(409, {"detail": "conflict"}), ErpPermanentError),
        (respond(422, {"detail": []}), ErpPermanentError),
        (respond(429), ErpTransientError),
        (respond(503), ErpTransientError),
        (respond(500), ErpUncertainError),
        (respond(502), ErpUncertainError),
        (respond(504), ErpUncertainError),
        (respond(201, {"unexpected": "shape"}), ErpUncertainError),
        (raise_error(httpx.ConnectError), ErpTransientError),
        (raise_error(httpx.ConnectTimeout), ErpTransientError),
        (raise_error(httpx.PoolTimeout), ErpTransientError),
        (raise_error(httpx.ReadTimeout), ErpUncertainError),
        (raise_error(httpx.WriteTimeout), ErpUncertainError),
        (raise_error(httpx.RemoteProtocolError), ErpUncertainError),
    ],
    ids=[
        "400-permanent",
        "409-permanent",
        "422-permanent",
        "429-transient",
        "503-transient",
        "500-uncertain",
        "502-uncertain",
        "504-uncertain",
        "malformed-body-uncertain",
        "connect-error-transient",
        "connect-timeout-transient",
        "pool-timeout-transient",
        "read-timeout-uncertain",
        "write-timeout-uncertain",
        "connection-dropped-uncertain",
    ],
)
def test_create_error_classification(handler: Handler, expected: type[Exception]) -> None:
    with make_client(handler) as client, pytest.raises(expected):
        client.create_order(**ORDER)


def test_transient_error_carries_retry_after() -> None:
    client = make_client(respond(429, headers={"Retry-After": "2"}))
    with client, pytest.raises(ErpTransientError) as exc_info:
        client.create_order(**ORDER)

    assert exc_info.value.retry_after == 2.0


def test_error_message_is_sanitized_and_truncated() -> None:
    client = make_client(respond(400, {"detail": "x" * 1000, "secret": "s3cr3t"}))
    with client, pytest.raises(ErpPermanentError) as exc_info:
        client.create_order(**ORDER)

    message = str(exc_info.value)
    assert message.startswith("ERP returned HTTP 400: xxx")
    assert len(message) < 250
    assert "s3cr3t" not in message


# --- get_order_by_external_id (unit) ------------------------------------------------


def test_lookup_found() -> None:
    with make_client(respond(200, ERP_BODY)) as client:
        assert client.get_order_by_external_id("ext-1") == ErpOrder.model_validate(ERP_BODY)


def test_lookup_not_found_returns_none() -> None:
    with make_client(respond(404, {"detail": "ERP order not found"})) as client:
        assert client.get_order_by_external_id("ext-1") is None


def test_lookup_escapes_external_id_in_path() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(404)

    with make_client(handler) as client:
        client.get_order_by_external_id("a/b c")

    assert paths == ["/erp/orders/by-external-id/a%2Fb%20c"]


@pytest.mark.parametrize(
    "handler",
    [respond(500), respond(503), respond(429), raise_error(httpx.ReadTimeout)],
    ids=["500", "503", "429", "read-timeout"],
)
def test_lookup_failures_are_transient(handler: Handler) -> None:
    """A lookup has no side effect: even a timeout is safe to retry."""
    with make_client(handler) as client, pytest.raises(ErpTransientError):
        client.get_order_by_external_id("ext-1")


# --- parse_retry_after ------------------------------------------------------------


def test_parse_retry_after() -> None:
    in_ten_seconds = format_datetime(datetime.now(UTC) + timedelta(seconds=10), usegmt=True)

    assert parse_retry_after(None) is None
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("-5") == 0.0
    assert parse_retry_after("soon") is None
    assert 8 <= (parse_retry_after(in_ten_seconds) or 0) <= 10


# --- against the real mock ERP over HTTP (acceptance scenarios) ----------------------

CLIENT_TIMEOUT = 0.3
SLOW_ERP_DELAY = 0.8  # > CLIENT_TIMEOUT


@pytest.fixture
def erp_client(erp_base_url: str) -> Iterator[ErpClient]:
    with ErpClient(erp_base_url, CLIENT_TIMEOUT) as client:
        yield client


def count_erp_orders(db_session: Session) -> int:
    return db_session.scalar(select(func.count()).select_from(ErpOrderRow)) or 0


@pytest.mark.integration
def test_create_then_lookup(erp_client: ErpClient) -> None:
    created = erp_client.create_order(**ORDER)

    assert erp_client.get_order_by_external_id("ext-1") == created
    assert erp_client.get_order_by_external_id("unknown") is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("http_400", ErpPermanentError),
        ("http_429", ErpTransientError),
        ("http_503", ErpTransientError),
    ],
)
def test_http_failure_modes(
    erp_client: ErpClient,
    erp_failures: FailureInjector,
    db_session: Session,
    mode: str,
    expected: type[Exception],
) -> None:
    erp_failures.configure(FailureConfig.model_validate({"mode": mode}))

    with pytest.raises(expected):
        erp_client.create_order(**ORDER)

    assert count_erp_orders(db_session) == 0


@pytest.mark.integration
def test_timeout_before_create_is_uncertain_and_nothing_exists(
    erp_client: ErpClient, erp_failures: FailureInjector
) -> None:
    erp_failures.configure(
        FailureConfig(mode="timeout_before_create", delay_seconds=SLOW_ERP_DELAY)
    )

    # From the client's point of view this is indistinguishable from the "after" case.
    with pytest.raises(ErpUncertainError):
        erp_client.create_order(**ORDER)

    assert erp_client.get_order_by_external_id("ext-1") is None


@pytest.mark.integration
def test_timeout_after_create_lookup_finds_the_single_order(
    erp_client: ErpClient, erp_failures: FailureInjector, db_session: Session
) -> None:
    """The core scenario: the ERP committed, but the response was lost."""
    erp_failures.configure(
        FailureConfig(mode="timeout_after_create", delay_seconds=SLOW_ERP_DELAY, count=1)
    )

    with pytest.raises(ErpUncertainError):
        erp_client.create_order(**ORDER)

    found = erp_client.get_order_by_external_id("ext-1")
    assert found is not None
    assert found.quantity == ORDER["quantity"]

    # Even a blind re-send would not duplicate: the ERP is idempotent on external_order_id.
    assert erp_client.create_order(**ORDER).erp_order_id == found.erp_order_id
    assert count_erp_orders(db_session) == 1
