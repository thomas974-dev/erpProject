from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import ErpFailureMode

ErpId = Annotated[str, Field(min_length=1, max_length=100)]


class ErpOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_order_id: ErpId
    customer_id: ErpId
    product_id: ErpId
    quantity: int = Field(gt=0, strict=True)


class ErpOrderRead(BaseModel):
    erp_order_id: str
    external_order_id: str
    customer_id: str
    product_id: str
    quantity: int
    created_at: datetime


class FailureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ErpFailureMode
    # Apply the failure to the next `count` create requests, then revert to normal.
    # None = until changed or reset.
    count: int | None = Field(default=None, ge=1)
    delay_seconds: float | None = Field(default=None, ge=0)


class FailureState(BaseModel):
    mode: ErpFailureMode
    remaining: int | None
    delay_seconds: float
