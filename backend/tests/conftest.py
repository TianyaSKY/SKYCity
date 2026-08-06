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
from app.database.models import (
    Conversation,
    ConversationMessage,
    Crop,
    Employment,
    GodAction,
    Inventory,
    Item,
    Job,
    LLMRun,
    Memory,
    Relationship,
    Save,
    ScheduledAction,
    Store,
    StoreProduct,
    Stock,
    StockHolding,
    TileStructure,
    Transaction,
    WorldEvent,
)
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
        for model in (
            LLMRun,
            WorldEvent,
            ScheduledAction,
            # M7 god action audit children.
            GodAction,
            # M9 save rows (FK worlds; deleted before worlds).
            Save,
            # M6 memory/relationship children.
            Memory,
            Relationship,
            # M5 economy children (SQLite does not cascade without PRAGMA FK on).
            Transaction,
            StockHolding,
            Stock,
            Inventory,
            Employment,
            StoreProduct,
            Store,
            Job,
            Item,
            # M14 structure overlay (FK agents/worlds; delete before both).
            TileStructure,
            # M15 crops (FK agents/worlds; delete before both).
            Crop,
            Agent,
            WorldLocation,
            ConversationMessage,
            Conversation,
            World,
        ):
            session.execute(delete(model))
        session.commit()
    finally:
        session.close()
    yield
