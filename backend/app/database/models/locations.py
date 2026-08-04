"""World location rows: seeded from the map's location objects per world."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class WorldLocation(Base):
    """A place agents can walk to; carries hours + capacity (R8/R15)."""

    __tablename__ = "locations"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location_type: Mapped[str] = mapped_column(String(32), nullable=False)
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    close_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WorldLocation(world_id={self.world_id!r}, location_id={self.location_id!r})"
