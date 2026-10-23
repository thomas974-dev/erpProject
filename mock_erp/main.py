"""Mock ERP: a separate service the integration talks to over HTTP only (port 8001)."""

import time
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from mock_erp.failures import FailureInjector
from mock_erp.models import ErpOrder
from mock_erp.schemas import ErpOrderCreate, ErpOrderRead, FailureConfig, FailureState

RETRY_AFTER_SECONDS = "1"
# The dev web UI is served by the Orders API (port 8000) and reads this service directly.
UI_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"]

app = FastAPI(
    title="Mock ERP",
    version="0.1.0",
    description="Simulated external ERP with deterministic failure injection (dev only).",
)
app.add_middleware(CORSMiddleware, allow_origins=UI_ORIGINS, allow_methods=["GET"])

_settings = get_settings()
failure_injector = FailureInjector(
    _settings.mock_erp_failure_mode, _settings.mock_erp_timeout_delay_seconds
)

SessionDep = Annotated[Session, Depends(get_db)]


class ErpConflictError(Exception):
    """external_order_id already exists with a different payload."""


def to_read(order: ErpOrder) -> ErpOrderRead:
    """Convert a stored ERP order into its API representation.

    Milestone 4.
    """
    return ErpOrderRead(
        erp_order_id=str(order.id),
        external_order_id=order.external_order_id,
        customer_id=order.customer_id,
        product_id=order.product_id,
        quantity=order.quantity,
        created_at=order.created_at,
    )


def _find(session: Session, external_order_id: str) -> ErpOrder | None:
    """Return the ERP order with this external_order_id, if any.

    Milestone 4.
    """
    return session.scalars(
        select(ErpOrder).where(ErpOrder.external_order_id == external_order_id)
    ).one_or_none()


def _same_payload(order: ErpOrder, payload: ErpOrderCreate) -> bool:
    """True if an existing ERP order matches the incoming payload.

    Milestone 4: same payload -> idempotent replay, different payload -> 409.
    """
    return (order.customer_id, order.product_id, order.quantity) == (
        payload.customer_id,
        payload.product_id,
        payload.quantity,
    )


def create_or_get(session: Session, payload: ErpOrderCreate) -> tuple[ErpOrder, bool]:
    """Idempotent create keyed by external_order_id. Returns (order, created).

    Milestone 4.
    """
    existing = _find(session, payload.external_order_id)
    if existing is None:
        order = ErpOrder(**payload.model_dump())
        session.add(order)
        try:
            session.commit()
            return order, True
        except IntegrityError:
            session.rollback()  # concurrent create won the unique constraint
            existing = _find(session, payload.external_order_id)
            if existing is None:
                raise
    if not _same_payload(existing, payload):
        raise ErpConflictError
    return existing, False


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness of the mock ERP.

    Milestone 4.
    """
    return {"status": "ok"}


@app.post(
    "/erp/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=ErpOrderRead,
    tags=["erp"],
    responses={
        200: {"description": "Order already existed (idempotent replay)"},
        409: {"description": "external_order_id exists with a different payload"},
    },
)
def create_erp_order(
    payload: ErpOrderCreate, response: Response, session: SessionDep
) -> ErpOrderRead:
    """POST /erp/orders: apply the injected failure, else create (201) or replay (200).

    Milestone 4. `timeout_after_create` commits the order *before* sleeping, to simulate a
    lost response.
    """
    mode, delay = failure_injector.consume()

    if mode == "http_400":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Simulated ERP validation error")
    if mode in ("http_429", "http_503"):
        code = 429 if mode == "http_429" else 503
        raise HTTPException(
            code, f"Simulated ERP {code}", headers={"Retry-After": RETRY_AFTER_SECONDS}
        )
    if mode == "timeout_before_create":
        time.sleep(delay)  # caller times out; nothing is created
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Simulated ERP timeout")

    try:
        order, created = create_or_get(session, payload)
    except ErpConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "external_order_id already exists with a different payload",
        ) from exc

    if mode == "timeout_after_create":
        time.sleep(delay)  # order is already committed: the response is "lost"

    if not created:
        response.status_code = status.HTTP_200_OK
    return to_read(order)


@app.get(
    "/erp/orders/by-external-id/{external_order_id}",
    response_model=ErpOrderRead,
    tags=["erp"],
    responses={404: {"description": "No ERP order with this external_order_id"}},
)
def get_erp_order_by_external_id(external_order_id: str, session: SessionDep) -> ErpOrderRead:
    """GET /erp/orders/by-external-id/{id}: lookup used for reconciliation; 404 if absent.

    Milestone 4.
    """
    order = _find(session, external_order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ERP order not found")
    return to_read(order)


@app.get("/erp/orders", response_model=list[ErpOrderRead], tags=["erp"])
def list_erp_orders(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ErpOrderRead]:
    """GET /erp/orders: the most recent ERP orders, newest first.

    Web UI (dev tool): shows which orders actually reached the ERP.
    """
    orders = session.scalars(
        select(ErpOrder).order_by(ErpOrder.created_at.desc(), ErpOrder.id.desc()).limit(limit)
    )
    return [to_read(order) for order in orders]


# --- Test-only failure injection controls ----------------------------------


@app.get("/admin/failure-mode", response_model=FailureState, tags=["admin"])
def get_failure_mode() -> FailureState:
    """Current failure injection state (test/demo only).

    Milestone 4.
    """
    return failure_injector.snapshot()


@app.put("/admin/failure-mode", response_model=FailureState, tags=["admin"])
def set_failure_mode(config: FailureConfig) -> FailureState:
    """Set the failure mode, optionally for a number of requests (test/demo only).

    Milestone 4.
    """
    return failure_injector.configure(config)


@app.delete("/admin/failure-mode", response_model=FailureState, tags=["admin"])
def reset_failure_mode() -> FailureState:
    """Back to the MOCK_ERP_FAILURE_MODE / MOCK_ERP_TIMEOUT_DELAY_SECONDS defaults.

    Milestone 4.
    """
    return failure_injector.reset()
