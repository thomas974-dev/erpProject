import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Index, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


# States from which an operator may trigger a manual retry.
RETRYABLE_STATUSES = (OrderStatus.FAILED, OrderStatus.UNKNOWN)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        # Serves GET /orders?status=...  ordered by newest first.
        Index("ix_orders_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    external_order_id: Mapped[str] = mapped_column(String(100), unique=True)
    # Unique but nullable: PostgreSQL allows any number of NULLs in a unique column.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    # SHA-256 of the normalized request payload, to detect key reuse with another payload.
    request_hash: Mapped[str | None] = mapped_column(String(64))
    # VARCHAR + CHECK rather than a native PostgreSQL ENUM: easier to evolve in migrations.
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            validate_strings=True,
        ),
        default=OrderStatus.PENDING,
    )
    erp_order_id: Mapped[str | None] = mapped_column(String(100))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # True while a create request may have reached the ERP without a confirmed outcome
    # (set before every create call). The next attempt must look the order up first.
    needs_reconciliation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
