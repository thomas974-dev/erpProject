import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate


class OrderNotFoundError(Exception):
    def __init__(self, order_id: UUID) -> None:
        super().__init__(f"Order {order_id} not found")
        self.order_id = order_id


class OrderService:
    """Order business logic. Storage is in-memory until Milestone 3 (PostgreSQL)."""

    def __init__(self) -> None:
        # Insertion-ordered; sync endpoints run in a thread pool, hence the lock.
        self._orders: dict[UUID, Order] = {}
        self._lock = threading.Lock()

    def create_order(self, data: OrderCreate) -> Order:
        order_id = uuid4()
        now = datetime.now(UTC)
        order = Order(
            id=order_id,
            customer_id=data.customer_id,
            product_id=data.product_id,
            quantity=data.quantity,
            # Stable identifier sent to the ERP on every attempt; never regenerated.
            external_order_id=str(order_id),
            status=OrderStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._orders[order.id] = order
        return order

    def get_order(self, order_id: UUID) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    def list_orders(
        self, *, status: OrderStatus | None, limit: int, offset: int
    ) -> tuple[list[Order], int]:
        """Return one page of orders (newest first) and the total matching count."""
        with self._lock:
            # Reverse insertion order == newest first, with no ties on equal timestamps.
            matching = [
                order
                for order in reversed(self._orders.values())
                if status is None or order.status == status
            ]
        return matching[offset : offset + limit], len(matching)


_order_service = OrderService()


def get_order_service() -> OrderService:
    """FastAPI dependency; overridden in tests to get an isolated service."""
    return _order_service
