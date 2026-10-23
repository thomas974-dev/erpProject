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
    """FastAPI dependency: an OrderService bound to the request's DB session.

    Milestone 2 (in-memory service), wired to PostgreSQL in Milestone 3.
    """
    return OrderService(session)


def get_order_dispatcher() -> OrderDispatcher:
    """Schedules background processing; overridden in tests to avoid needing Redis.

    Milestone 5: the API enqueues a Celery task instead of calling the ERP itself.
    """
    return enqueue_order_processing


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
OrderDispatcherDep = Annotated[OrderDispatcher, Depends(get_order_dispatcher)]
