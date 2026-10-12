from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.models.order import OrderStatus
from app.schemas.order import OrderAccepted, OrderCreate, OrderList, OrderRead
from app.services.order_service import OrderNotFoundError, OrderService, get_order_service

router = APIRouter(prefix="/orders", tags=["orders"])

OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=OrderAccepted)
def create_order(
    payload: OrderCreate, request: Request, response: Response, service: OrderServiceDep
) -> OrderAccepted:
    order = service.create_order(payload)
    status_url = request.app.url_path_for("get_order", order_id=str(order.id))
    response.headers["Location"] = status_url
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
