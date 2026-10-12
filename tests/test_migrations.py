import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def test_models_and_migrations_are_in_sync(db_engine: Engine, alembic_config: Config) -> None:
    """Fails if a model was changed without generating the matching migration."""
    command.check(alembic_config)
