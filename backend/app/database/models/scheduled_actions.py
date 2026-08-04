"""Scheduler queue rows: actions due at a world_time, dispatched by the engine."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _new_action_id() -> str:
    return f"act_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScheduledAction(Base):
    """One future world-engine callback (move/wait completion, capacity recheck)."""

    __tablename__ = "scheduled_actions"
    __table_args__ = (Index("ix_scheduled_world_due", "world_id", "due_at"),)

    action_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_new_action_id)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    due_at: Mapped[int] = mapped_column(Integer, nullable=False)  # world_time
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScheduledAction(action_id={self.action_id!r}, type={self.action_type!r}, "
            f"due_at={self.due_at}, agent={self.agent_id!r})"
        )
