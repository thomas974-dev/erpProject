from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus

# Whitespace is stripped before the length check, so "   " is rejected.
ExternalId = Annotated[str, Field(min_length=1, max_length=100)]


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: ExternalId
    product_id: ExternalId
    # strict: reject "10", 10.5 and true instead of silently coercing them.
    quantity: int = Field(gt=0, strict=True)


class OrderAccepted(BaseModel):
    """Response of POST /orders (202): the order is stored, processing happens later."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: OrderStatus
    external_order_id: str
    created_at: datetime
    status_url: str


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: str
    product_id: str
    quantity: int
    external_order_id: str
    status: OrderStatus
    erp_order_id: str | None
    attempt_count: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class OrderList(BaseModel):
    items: list[OrderRead]
    total: int
    limit: int
    offset: int
