import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.order import RETRYABLE_STATUSES, Order, OrderStatus, utc_now
from app.schemas.order import OrderCreate


class OrderNotFoundError(Exception):
    def __init__(self, order_id: uuid.UUID) -> None:
        """Raised for an unknown order ID; mapped to 404 by the API.

        Milestone 2.
        """
        super().__init__(f"Order {order_id} not found")
        self.order_id = order_id


class IdempotencyConflictError(Exception):
    """The Idempotency-Key was already used with a different payload."""


class OrderNotRetryableError(Exception):
    def __init__(self, order_id: uuid.UUID, status: OrderStatus) -> None:
        """Raised when retrying an order that is not FAILED/UNKNOWN; mapped to 409.

        Milestone 6.
        """
        super().__init__(f"Order {order_id} is {status}; only FAILED or UNKNOWN can be retried")
        self.status = status


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    order: Order
    created: bool  # False when an existing order was returned for a replayed request


def compute_request_hash(data: OrderCreate) -> str:
    """SHA-256 of the validated (hence normalized) payload, serialized canonically.

    Milestone 3: detects an Idempotency-Key reused with a different payload.
    """
    canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class OrderService:
    def __init__(self, session: Session) -> None:
        """API-side business logic for orders, on one DB session.

        Milestone 2 (in memory), PostgreSQL since Milestone 3.
        """
        self._session = session

    def create_order(
        self, data: OrderCreate, idempotency_key: str | None = None
    ) -> CreateOrderResult:
        """Store a new PENDING order, or return the original one for a replayed key.

        Milestone 2 (creation), 3 (idempotency, race-safe through the unique constraint).
        Raises IdempotencyConflictError if the key was used with another payload.
        """
        request_hash = compute_request_hash(data)

        if idempotency_key is not None:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._replay(existing, request_hash)

        order_id = uuid.uuid4()
        now = utc_now()  # one clock read: a new order has created_at == updated_at
        order = Order(
            id=order_id,
            customer_id=data.customer_id,
            product_id=data.product_id,
            quantity=data.quantity,
            # Stable identifier sent to the ERP on every attempt; never regenerated.
            external_order_id=str(order_id),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=OrderStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self._session.add(order)
        try:
            self._session.commit()
        except IntegrityError:
            # A concurrent request with the same key committed first: the unique
            # constraint, not the lookup above, is what guarantees a single order.
            self._session.rollback()
            existing = self._find_by_idempotency_key(idempotency_key) if idempotency_key else None
            if existing is None:
                raise
            return self._replay(existing, request_hash)
        return CreateOrderResult(order=order, created=True)

    def get_order(self, order_id: uuid.UUID) -> Order:
        """Return the order or raise OrderNotFoundError.

        Milestone 2, persisted in Milestone 3.
        """
        order = self._session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    def retry_order(self, order_id: uuid.UUID) -> Order:
        """Move a FAILED/UNKNOWN order back to PENDING. Atomic: when two retry requests race,
        exactly one wins, so at most one new task is scheduled.

        Milestone 6: manual retry. needs_reconciliation is left untouched on purpose.
        """
        order = self._session.scalars(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(RETRYABLE_STATUSES))
            .values(status=OrderStatus.PENDING, updated_at=utc_now())
            .returning(Order)
        ).one_or_none()
        self._session.commit()
        if order is None:
            current = self.get_order(order_id)  # raises OrderNotFoundError
            raise OrderNotRetryableError(order_id, current.status)
        return order

    def list_orders(
        self, *, status: OrderStatus | None, limit: int, offset: int
    ) -> tuple[list[Order], int]:
        """Return one page of orders (newest first) and the total matching count.

        Milestone 2, persisted in Milestone 3.
        """
        filters = [Order.status == status] if status is not None else []
        page = self._session.scalars(
            select(Order)
            .where(*filters)
            # id breaks ties so pagination is deterministic.
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        total = self._session.scalar(select(func.count()).select_from(Order).where(*filters))
        return list(page), total or 0

    def _find_by_idempotency_key(self, idempotency_key: str) -> Order | None:
        """Return the order created with this Idempotency-Key, if any.

        Milestone 3.
        """
        return self._session.scalars(
            select(Order).where(Order.idempotency_key == idempotency_key)
        ).one_or_none()

    @staticmethod
    def _replay(existing: Order, request_hash: str) -> CreateOrderResult:
        """Same key + same payload -> original order; different payload -> conflict.

        Milestone 3: client idempotency (no second order, no second task).
        """
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request payload"
            )
        return CreateOrderResult(order=existing, created=False)
