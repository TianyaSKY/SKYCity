"""World aggregate root: one row per simulation world."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base

DEFAULT_WORLD_TIME = 480  # 08:00
DEFAULT_SPEED = 1
DEFAULT_WEATHER = "clear"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class World(Base):
    """A simulation world (clock, speed, pause flag, weather)."""

    __tablename__ = "worlds"

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    world_time: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_WORLD_TIME)
    speed: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_SPEED)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weather: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_WEATHER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"World(world_id={self.world_id!r}, world_time={self.world_time}, "
            f"speed={self.speed}, paused={self.paused}, weather={self.weather!r})"
        )
