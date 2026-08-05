"""Database models package: every ORM model used by the world engine.

Importing this package registers all tables on Base.metadata so alembic
autogenerate and create_all see the full schema.
"""

from app.database.models.agents import Agent
from app.database.models.conversations import Conversation, ConversationMessage
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Employment, Job
from app.database.models.llm_runs import LLMRun
from app.database.models.locations import WorldLocation
from app.database.models.memories import Memory
from app.database.models.relationships import Relationship
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.database.session import Base

__all__ = [
    "Agent",
    "Base",
    "Conversation",
    "ConversationMessage",
    "Employment",
    "Inventory",
    "Item",
    "Job",
    "LLMRun",
    "Memory",
    "Relationship",
    "ScheduledAction",
    "Store",
    "StoreProduct",
    "Transaction",
    "World",
    "WorldEvent",
    "WorldLocation",
]
