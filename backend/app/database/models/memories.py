"""Memory rows: one agent's working / episodic / semantic memories (M6).

Memories are written only from observed world events (MemoryRecorder) or the
daily reflection; ``created_at`` is the world_time when the memory was formed
so recency scoring is comparable across agents and worlds.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Memory(Base):
    """One memory belonging to one agent in one world."""

    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_world_agent", "world_id", "agent_id"),)

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False)  # working|episodic|semantic
    text: Mapped[str] = mapped_column(String(512), nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    entities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    keywords_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)  # world_time
    last_recalled_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Memory({self.memory_id!r}, {self.agent_id!r}, {self.memory_type!r}, "
            f"importance={self.importance})"
        )
