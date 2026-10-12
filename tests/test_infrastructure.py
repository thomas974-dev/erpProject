"""Connectivity checks for the Docker Compose services (Milestone 1)."""

import pytest
import redis
from sqlalchemy import create_engine, text

from app.core.config import get_settings

pytestmark = pytest.mark.integration


def test_postgres_is_reachable() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
    finally:
        engine.dispose()

    assert version.startswith("16")


def test_redis_is_reachable() -> None:
    client = redis.Redis.from_url(get_settings().celery_broker_url, socket_timeout=2)
    try:
        assert client.ping() is True
    finally:
        client.close()
