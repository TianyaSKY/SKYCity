"""World event rows: the persisted, replayable event log (event-protocol.md §4)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorldEvent(Base):
    """One envelope in a world's event stream; sequence is per-world monotonic."""

    __tablename__ = "world_events"
    __table_args__ = (
        UniqueConstraint("world_id", "sequence", name="uq_world_events_world_sequence"),
        Index("ix_world_events_world_seq", "world_id", "sequence"),
    )

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    world_time: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WorldEvent(world_id={self.world_id!r}, seq={self.sequence}, type={self.type!r})"
