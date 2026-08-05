"""Test configuration.

Pins the world engine to an isolated test database (never the dev
ai_tiny_world.db) and creates/drops the schema once per session. The env var
must be set before any app module is imported (settings are cached).
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///./test_ai_tiny_world.db"

import pytest  # noqa: E402
from sqlalchemy import delete

from app.database import models  # noqa: E402,F401  (populates Base.metadata)
from app.database.models import LLMRun, ScheduledAction, WorldEvent
from app.database.models.agents import Agent
from app.database.models.locations import WorldLocation
from app.database.models.worlds import World
from app.database.session import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database_schema() -> None:
    """Fresh schema for the whole test session, dropped afterwards."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_db(_database_schema) -> None:
    """Wipe every world (children first, dependency order) before each test,
    so world numbering and event sequences restart at 1."""
    session = SessionLocal()
    try:
        for model in (LLMRun, WorldEvent, ScheduledAction, Agent, WorldLocation, World):
            session.execute(delete(model))
        session.commit()
    finally:
        session.close()
    yield
