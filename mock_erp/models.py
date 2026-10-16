import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.order import utc_now


class ErpOrder(Base):
    """An order as recorded by the (simulated) external ERP."""

    __tablename__ = "erp_orders"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The caller's stable identifier: makes ERP creation idempotent.
    external_order_id: Mapped[str] = mapped_column(String(100), unique=True)
    customer_id: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
