import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.order_service import OrderService
from app.workers.tasks import enqueue_order_processing

OrderDispatcher = Callable[[uuid.UUID], None]


def get_order_service(session: Annotated[Session, Depends(get_db)]) -> OrderService:
    return OrderService(session)


def get_order_dispatcher() -> OrderDispatcher:
    """Schedules background processing; overridden in tests to avoid needing Redis."""
    return enqueue_order_processing


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
OrderDispatcherDep = Annotated[OrderDispatcher, Depends(get_order_dispatcher)]
