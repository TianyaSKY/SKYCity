"""Relationship rows: directional, system-computed agent pair bonds (M6).

One row per (world, source_agent_id, target_agent_id): the source agent's
current feelings toward the target. Deltas are computed by the
RelationshipService from observed events (never returned by the LLM). All
axes are integers; familiarity/trust/affection/resentment clamp to 0..100,
debt to 0..1000.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Relationship(Base):
    """The source agent's relationship state toward the target agent."""

    __tablename__ = "relationships"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    source_agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    familiarity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trust: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affection: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resentment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    debt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # world_time

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Relationship({self.source_agent_id!r}->{self.target_agent_id!r}, "
            f"fam={self.familiarity}, aff={self.affection})"
        )
