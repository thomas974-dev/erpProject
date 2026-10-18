import uuid
from typing import Any

from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.integrations.erp_client import ErpClient
from app.services.order_processor import OrderProcessor
from app.workers.celery_app import celery_app


@worker_process_init.connect
def _reset_db_pool(**_: Any) -> None:
    # Prefork pool: never share the parent's pooled DB connections with child processes.
    engine.dispose(close=False)


@celery_app.task(name="orders.process_order")
def process_order(order_id: str) -> None:
    """Takes an order ID only; the current data is reloaded from PostgreSQL."""
    with SessionLocal() as session, ErpClient.from_settings(get_settings()) as erp:
        OrderProcessor(session, erp).process(uuid.UUID(order_id))


def enqueue_order_processing(order_id: uuid.UUID) -> None:
    process_order.delay(str(order_id))
