"""Database models package: every ORM model used by the world engine.

Importing this package registers all tables on Base.metadata so alembic
autogenerate and create_all see the full schema.
"""

from app.database.models.agents import Agent
from app.database.models.companies import (
    Company,
    CompanyInventory,
    CompanyTransaction,
    EmploymentContract,
    JobApplication,
    JobOpening,
    LeaveRequest,
    Position,
    WorkShift,
)
from app.database.models.conversations import Conversation, ConversationMessage
from app.database.models.crops import Crop
from app.database.models.god_actions import GodAction
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Job, WorkHistory
from app.database.models.llm_runs import LLMRun
from app.database.models.locations import WorldLocation
from app.database.models.memories import Memory
from app.database.models.relationships import Relationship
from app.database.models.saves import Save
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.stocks import Stock, StockHolding
from app.database.models.structures import TileStructure
from app.database.models.transactions import Transaction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.database.session import Base

__all__ = [
    "Agent",
    "Base",
    "Company",
    "CompanyInventory",
    "CompanyTransaction",
    "Conversation",
    "ConversationMessage",
    "Crop",
    "EmploymentContract",
    "GodAction",
    "Inventory",
    "Item",
    "Job",
    "JobApplication",
    "JobOpening",
    "LLMRun",
    "LeaveRequest",
    "Memory",
    "Position",
    "Relationship",
    "Save",
    "ScheduledAction",
    "Store",
    "StoreProduct",
    "Stock",
    "StockHolding",
    "TileStructure",
    "Transaction",
    "WorkHistory",
    "WorkShift",
    "World",
    "WorldEvent",
    "WorldLocation",
]
