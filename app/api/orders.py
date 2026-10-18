import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from kombu.exceptions import OperationalError as BrokerError

from app.api.dependencies import OrderDispatcherDep, OrderServiceDep
from app.models.order import OrderStatus
from app.schemas.order import OrderAccepted, OrderCreate, OrderList, OrderRead
from app.services.order_service import IdempotencyConflictError, OrderNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OrderAccepted,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency-Key already used with a different payload"
        }
    },
)
def create_order(
    payload: OrderCreate,
    request: Request,
    response: Response,
    service: OrderServiceDep,
    dispatch: OrderDispatcherDep,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> OrderAccepted:
    try:
        result = service.create_order(payload, idempotency_key=idempotency_key)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    order = result.order
    if result.created:  # a replayed request must not schedule a second task
        try:
            dispatch(order.id)
        except BrokerError:
            # Known MVP limitation (no outbox): the order is committed but not queued and
            # stays PENDING until a recovery mechanism re-publishes it. See README.
            logger.exception("event=order_enqueue_failed order_id=%s", order.id)
    status_url = request.app.url_path_for("get_order", order_id=str(order.id))
    response.headers["Location"] = status_url
    if not result.created:
        response.headers["Idempotent-Replayed"] = "true"
    return OrderAccepted(
        id=order.id,
        status=order.status,
        external_order_id=order.external_order_id,
        created_at=order.created_at,
        status_url=status_url,
    )


@router.get("", response_model=OrderList)
def list_orders(
    service: OrderServiceDep,
    status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderList:
    orders, total = service.list_orders(status=status_filter, limit=limit, offset=offset)
    return OrderList(
        items=[OrderRead.model_validate(order) for order in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{order_id}",
    response_model=OrderRead,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Order not found"}},
)
def get_order(order_id: UUID, service: OrderServiceDep) -> OrderRead:
    try:
        order = service.get_order(order_id)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return OrderRead.model_validate(order)
