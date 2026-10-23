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
    """Readiness check: PostgreSQL answers `SELECT 1`. Raises on failure.

    Milestone 7.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def check_broker() -> None:
    """Readiness check: Redis (the Celery broker) answers PING. Raises on failure.

    Milestone 7.
    """
    client = redis.Redis.from_url(
        get_settings().celery_broker_url, socket_timeout=1, socket_connect_timeout=1
    )
    try:
        client.ping()
    finally:
        client.close()


def get_readiness_checks() -> dict[str, ReadinessCheck]:
    """FastAPI dependency listing the checks run by /ready (overridden in tests).

    Milestone 7.
    """
    return {"database": check_database, "broker": check_broker}


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the API process is running. Does not check PostgreSQL or Redis.

    Milestone 2.
    """
    return {"status": "ok"}


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "A dependency is down"}},
)
def ready(
    response: Response,
    checks: Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)],
) -> dict[str, Any]:
    """Readiness: PostgreSQL and Redis (the broker) both answer.

    Milestone 7: returns 503 with per-dependency results when one is down.
    """
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
