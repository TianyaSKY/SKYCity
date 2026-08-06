"""Crop rows: planted seeds growing on the farm (M15, R23).

A row exists from planting (stage 0, seed consumed) until harvest (row
deleted) or god removal. The composite PK (world_id, col, row) is the R23.3
occupancy guard — exactly one crop per cell, and crops are mutually exclusive
with tile_structures (cross-table check in CropService).

Growth is driven by the world scheduler: plant schedules a "crop_grow"
callback at next_stage_at; the handler advances the stage, publishes
crop_grown and schedules the next stage. The callback is idempotent: it only
fires when crop.stage == payload.stage AND crop.next_stage_at == due time,
so god stage rewrites or harvests make stale callbacks no-ops (R23.5).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Crop(Base):
    """One planted crop on a farm cell."""

    __tablename__ = "crops"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "planted_by"],
            ["agents.world_id", "agents.agent_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    col: Mapped[int] = mapped_column(Integer, primary_key=True)
    row: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The seed item that was planted (== the crop's key in crops.json).
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    planted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    planted_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # 0-based growth stage index (0 = just planted, final = harvestable).
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    # World time the current stage ends (the pending crop_grow due time).
    next_stage_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Crop({self.item_id!r} @ ({self.col},{self.row}) "
            f"stage={self.stage} next={self.next_stage_at})"
        )
