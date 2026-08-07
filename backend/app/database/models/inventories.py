"""Inventory rows: how much of each item an agent carries (M5 economy)."""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Inventory(Base):
    """One (agent, item) stack. quantity 0 rows are removed on mutation."""

    __tablename__ = "inventories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "agent_id"],
            ["agents.world_id", "agents.agent_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["world_id", "item_id"],
            ["items.world_id", "items.item_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Inventory(world_id={self.world_id!r}, agent={self.agent_id!r}, item={self.item_id!r}, qty={self.quantity})"
