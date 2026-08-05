"""Save rows: one row per archived world snapshot (M9).

``payload_json`` carries the full serialized state (docs/world-rules.md R17);
``map_version`` mirrors the payload so save listings can detect map drift
without loading the payload.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Save(Base):
    """One archived world snapshot, restorable into a NEW world."""

    __tablename__ = "saves"

    save_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    map_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # World time (game minutes) the save was taken at.
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Save(save_id={self.save_id!r}, world={self.world_id!r}, at={self.created_at})"
