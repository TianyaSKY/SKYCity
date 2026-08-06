"""TileStructure rows: agent-built structures placed on the map (M14, R22).

A row exists from the moment a build starts (status="building", materials
pre-deducted) until it is completed (status="built") or removed by the god
view. The composite PK (world_id, col, row) is the R22.3 occupancy guard:
exactly one structure (building or built) may claim a cell, so concurrent
builds on the same tile are rejected by the unique constraint.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class TileStructure(Base):
    """One cell of a blueprint footprint on the map overlay."""

    __tablename__ = "tile_structures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "owner_agent_id"],
            ["agents.world_id", "agents.agent_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    col: Mapped[int] = mapped_column(Integer, primary_key=True)
    row: Mapped[int] = mapped_column(Integer, primary_key=True)
    blueprint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # "building" (materials pre-deducted, completion pending) | "built".
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # World time when the structure completed (None while status="building").
    built_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The deducted materials dict {item_id: qty} — refunded on god interrupt
    # (R22.2 proportional) or when completion re-validation fails.
    materials_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TileStructure({self.blueprint_id!r} @ ({self.col},{self.row}) "
            f"status={self.status!r} owner={self.owner_agent_id!r})"
        )
