from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Deterministic constraint names, so Alembic migrations can reference (and drop) them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Connections are opened lazily, on first use. pool_pre_ping discards stale connections
# (e.g. after a PostgreSQL restart) instead of failing the request.
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},  # fail fast if PostgreSQL is unreachable
)

# expire_on_commit=False: objects stay readable after commit without an extra SELECT.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards.

    Milestone 3: PostgreSQL persistence (SQLAlchemy sessions).
    """
    with SessionLocal() as session:
        yield session
