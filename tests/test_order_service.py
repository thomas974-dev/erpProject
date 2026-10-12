import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.order import Order
from app.schemas.order import OrderCreate
from app.services.order_service import (
    IdempotencyConflictError,
    OrderService,
    compute_request_hash,
)

pytestmark = pytest.mark.integration

PAYLOAD = OrderCreate(customer_id="CLIENT-123", product_id="PRODUCT-456", quantity=10)


def test_request_hash_is_stable_and_payload_sensitive() -> None:
    same = OrderCreate(customer_id=" CLIENT-123", product_id="PRODUCT-456", quantity=10)
    other = OrderCreate(customer_id="CLIENT-123", product_id="PRODUCT-456", quantity=11)

    assert compute_request_hash(PAYLOAD) == compute_request_hash(same)
    assert compute_request_hash(PAYLOAD) != compute_request_hash(other)


def test_created_order_is_persisted_with_defaults(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        order = OrderService(session).create_order(PAYLOAD).order

    # Read back through a brand-new session: the data comes from PostgreSQL.
    with session_factory() as session:
        stored = session.get_one(Order, order.id)

    assert stored.status == "PENDING"
    assert stored.external_order_id == str(order.id)
    assert stored.attempt_count == 0
    assert stored.created_at.tzinfo is not None


def make_lookup_stale_once(service: OrderService, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the race window: the first key lookup ran before the competing commit."""
    real_lookup = service._find_by_idempotency_key
    stale = [True]

    def lookup(key: str) -> Order | None:
        if stale:
            stale.pop()
            return None
        return real_lookup(key)

    monkeypatch.setattr(service, "_find_by_idempotency_key", lookup)


def test_race_on_same_key_is_resolved_by_unique_constraint(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as session:
        winner = OrderService(session).create_order(PAYLOAD, idempotency_key="same-key")

    with session_factory() as session:
        loser = OrderService(session)
        make_lookup_stale_once(loser, monkeypatch)
        # INSERT violates uq_orders_idempotency_key -> rollback -> replay the winner.
        result = loser.create_order(PAYLOAD, idempotency_key="same-key")

    assert result.created is False
    assert result.order.id == winner.order.id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_race_on_same_key_with_different_payload_is_a_conflict(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as session:
        OrderService(session).create_order(PAYLOAD, idempotency_key="same-key")

    with session_factory() as session:
        loser = OrderService(session)
        make_lookup_stale_once(loser, monkeypatch)
        with pytest.raises(IdempotencyConflictError):
            loser.create_order(PAYLOAD.model_copy(update={"quantity": 99}), "same-key")
