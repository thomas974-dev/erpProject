import logging
from collections.abc import Callable
from typing import Annotated, Any

import redis
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine
from app.core.logging import log_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

ReadinessCheck = Callable[[], None]


def check_database() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def check_broker() -> None:
    client = redis.Redis.from_url(
        get_settings().celery_broker_url, socket_timeout=1, socket_connect_timeout=1
    )
    try:
        client.ping()
    finally:
        client.close()


def get_readiness_checks() -> dict[str, ReadinessCheck]:
    return {"database": check_database, "broker": check_broker}


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the API process is running. Does not check PostgreSQL or Redis."""
    return {"status": "ok"}


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "A dependency is down"}},
)
def ready(
    response: Response,
    checks: Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)],
) -> dict[str, Any]:
    """Readiness: PostgreSQL and Redis (the broker) both answer."""
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
            results[name] = "ok"
        except Exception as exc:
            # Details go to the logs only: never expose internal errors to callers.
            log_event(
                logger,
                "readiness_check_failed",
                level=logging.WARNING,
                check=name,
                error=type(exc).__name__,
            )
            results[name] = "unavailable"
    is_ready = all(result == "ok" for result in results.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if is_ready else "not_ready", "checks": results}
