"""Shared fixtures: a dedicated PostgreSQL test database, migrated with Alembic."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import get_db
from app.main import app
from mock_erp.failures import FailureInjector
from mock_erp.main import app as erp_app
from mock_erp.main import failure_injector


def _test_database_url() -> URL:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test")


def _create_database_if_missing(url: URL) -> None:
    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin_engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    url = _test_database_url()
    _create_database_if_missing(url)
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return config


@pytest.fixture(scope="session")
def db_engine(alembic_config: Config) -> Iterator[Engine]:
    # Start from an empty schema each run: also exercises every downgrade() and upgrade().
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    engine = create_engine(_test_database_url())
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE orders, erp_orders"))
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- Mock ERP ------------------------------------------------------------------


@pytest.fixture
def erp_failures() -> Iterator[FailureInjector]:
    """The mock ERP's failure injector, reset to `normal` before and after each test."""
    failure_injector.reset()
    yield failure_injector
    failure_injector.reset()


@pytest.fixture
def erp_api(
    session_factory: sessionmaker[Session], erp_failures: FailureInjector
) -> Iterator[TestClient]:
    """In-process client for the mock ERP app (no real network)."""

    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    # Restore (not clear) afterwards: the session-scoped server relies on its own override.
    previous = dict(erp_app.dependency_overrides)
    erp_app.dependency_overrides[get_db] = override_get_db
    with TestClient(erp_app) as test_client:
        yield test_client
    erp_app.dependency_overrides.clear()
    erp_app.dependency_overrides.update(previous)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def mock_erp_server(db_engine: Engine) -> Iterator[str]:
    """The mock ERP served by a real uvicorn in a background thread: real sockets, real
    timeouts. Yields its base URL."""
    test_sessions = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_get_db() -> Iterator[Session]:
        with test_sessions() as session:
            yield session

    erp_app.dependency_overrides[get_db] = override_get_db
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(erp_app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock ERP server did not start")
        time.sleep(0.01)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    erp_app.dependency_overrides.clear()


@pytest.fixture
def erp_base_url(
    mock_erp_server: str,
    session_factory: sessionmaker[Session],
    erp_failures: FailureInjector,
) -> str:
    """Base URL of the running mock ERP, with empty tables and `normal` mode."""
    return mock_erp_server
