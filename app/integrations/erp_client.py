"""HTTP client for the ERP, with explicit classification of every failure.

Every error raised is one of:
- ErpTransientError: nothing was done, retrying later is safe (429/503, connection refused).
- ErpPermanentError: the ERP rejected the request; retrying will not help (4xx).
- ErpUncertainError: the ERP *may* have created the order (timeout after sending, 5xx).
  Never retry a create after this without first looking the order up.
"""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings

MAX_ERROR_DETAIL_LENGTH = 200
# The ERP explicitly says "not processed, try again later".
TRANSIENT_STATUS_CODES = frozenset({429, 503})
# Raised before any byte of the request reached the ERP: no side effect possible.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class ErpError(Exception):
    """Base class. Messages are sanitized and safe to store in orders.last_error."""


class ErpTransientError(ErpError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Certainly not processed; `retry_after` comes from the Retry-After header.

        Milestone 4.
        """
        super().__init__(message)
        self.retry_after = retry_after


class ErpPermanentError(ErpError):
    pass


class ErpUncertainError(ErpError):
    pass


class ErpOrder(BaseModel):
    """ERP response, validated: external data is never trusted as-is."""

    erp_order_id: str
    external_order_id: str
    customer_id: str
    product_id: str
    quantity: int


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date.

    Milestone 4.
    """
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def describe_response(response: httpx.Response) -> str:
    """Short, sanitized description: status code and a truncated `detail` only.

    Milestone 4: safe to store in orders.last_error.
    """
    message = f"ERP returned HTTP {response.status_code}"
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail:
        message += f": {detail[:MAX_ERROR_DETAIL_LENGTH]}"
    return message


class ErpClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Synchronous HTTPX client; `transport` lets unit tests use httpx.MockTransport.

        Milestone 4.
        """
        self._http = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build a client from ERP_BASE_URL and ERP_HTTP_TIMEOUT_SECONDS.

        Milestone 4.
        """
        return cls(settings.erp_base_url, settings.erp_http_timeout_seconds)

    def close(self) -> None:
        """Close the underlying connection pool.

        Milestone 4.
        """
        self._http.close()

    def __enter__(self) -> Self:
        """Context manager support, so the worker always closes the client.

        Milestone 4.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when leaving the `with` block.

        Milestone 4.
        """
        self.close()

    def create_order(
        self, *, external_order_id: str, customer_id: str, product_id: str, quantity: int
    ) -> ErpOrder:
        """Create (or idempotently re-fetch) the ERP order for `external_order_id`.

        Milestone 4: classifies every failure as transient, permanent or uncertain.
        """
        payload = {
            "external_order_id": external_order_id,
            "customer_id": customer_id,
            "product_id": product_id,
            "quantity": quantity,
        }
        try:
            response = self._http.post("/erp/orders", json=payload)
        except NOT_SENT_ERRORS as exc:
            raise ErpTransientError(f"ERP unreachable ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            # Sent, but no complete response: the ERP may have committed the order.
            raise ErpUncertainError(f"No response from ERP ({type(exc).__name__})") from exc

        code = response.status_code
        if code in (200, 201):
            return self._parse(response, error=ErpUncertainError)
        if code in TRANSIENT_STATUS_CODES:
            raise ErpTransientError(
                describe_response(response),
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if 400 <= code < 500:
            raise ErpPermanentError(describe_response(response))
        # Other 5xx (500, 502, 504...): the failure may have happened after the commit.
        raise ErpUncertainError(describe_response(response))

    def get_order_by_external_id(self, external_order_id: str) -> ErpOrder | None:
        """Look the order up. Read-only, so every transport failure is transient.

        Milestone 4 (lookup endpoint), used for reconciliation in Milestone 6.
        """
        path = f"/erp/orders/by-external-id/{quote(external_order_id, safe='')}"
        try:
            response = self._http.get(path)
        except httpx.TransportError as exc:
            raise ErpTransientError(f"ERP lookup failed ({type(exc).__name__})") from exc

        code = response.status_code
        if code == 200:
            return self._parse(response, error=ErpTransientError)
        if code == 404:
            return None
        if code in TRANSIENT_STATUS_CODES or code >= 500:
            raise ErpTransientError(
                describe_response(response),
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        raise ErpPermanentError(describe_response(response))

    @staticmethod
    def _parse(response: httpx.Response, *, error: type[ErpError]) -> ErpOrder:
        """Validate the ERP response body; raise `error` if it is malformed.

        Milestone 4.
        """
        try:
            return ErpOrder.model_validate_json(response.content)
        except ValidationError as exc:
            raise error(f"Malformed ERP response (HTTP {response.status_code})") from exc
