"""Test configuration.

Pins the world engine to an isolated test database (never the dev
ai_tiny_world.db) and creates/drops the schema once per session. The env var
must be set before any app module is imported (settings are cached).
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///./test_ai_tiny_world.db"

import pytest  # noqa: E402

from app.database import models  # noqa: E402,F401  (populates Base.metadata)
from app.database.session import Base, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database_schema() -> None:
    """Fresh schema for the whole test session, dropped afterwards."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
