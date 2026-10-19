import uuid
from typing import Any

from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.integrations.erp_client import ErpClient
from app.services.order_processor import OrderProcessor, RetryPolicy
from app.workers.celery_app import celery_app


@worker_process_init.connect
def _reset_db_pool(**_: Any) -> None:
    # Prefork pool: never share the parent's pooled DB connections with child processes.
    engine.dispose(close=False)


@celery_app.task(name="orders.process_order")
def process_order(order_id: str) -> None:
    """Takes an order ID only; the current data is reloaded from PostgreSQL."""
    settings = get_settings()
    with SessionLocal() as session, ErpClient.from_settings(settings) as erp:
        processor = OrderProcessor(session, erp, RetryPolicy.from_settings(settings))
        outcome = processor.process(uuid.UUID(order_id))

    if outcome is not None and outcome.retry_in is not None:
        # The order is already committed as PENDING/UNKNOWN; schedule the next attempt.
        # (If this publish fails, the order stays there: same limitation as the API.)
        process_order.apply_async(args=[order_id], countdown=outcome.retry_in)


def enqueue_order_processing(order_id: uuid.UUID) -> None:
    process_order.delay(str(order_id))
