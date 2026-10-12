from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


# Temporary in-memory domain object (Milestone 2).
# Replaced by a SQLAlchemy model with the same attributes in Milestone 3.
@dataclass(kw_only=True, slots=True)
class Order:
    id: UUID
    customer_id: str
    product_id: str
    quantity: int
    external_order_id: str
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    erp_order_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
