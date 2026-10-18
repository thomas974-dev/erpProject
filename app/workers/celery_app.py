from celery import Celery

from app.core.config import get_settings

celery_app = Celery(
    "order_to_erp",
    broker=get_settings().celery_broker_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Order status lives in PostgreSQL: no result backend needed.
    task_ignore_result=True,
    # Ack after the task ran, so a crashed worker's message is redelivered
    # (at-least-once). Safe because the worker claims orders atomically.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Tasks are I/O-bound (HTTP + SQL, GIL released while waiting): threads fit. Also,
    # prefork is broken on macOS since billiard defaults to `spawn` there (celery#9894).
    worker_pool="threads",
    worker_concurrency=4,
    # One message at a time per worker thread: no order waits behind a slow one.
    worker_prefetch_multiplier=1,
    # If Redis is down, fail fast at publish time instead of hanging the HTTP request.
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2},
    broker_connection_retry_on_startup=True,
)
