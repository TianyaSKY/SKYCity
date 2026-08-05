"""God action audit rows (M7): every god intervention, success or failure.

One row per god-actions call; ``parameters_json`` carries the raw request,
``result_json`` the outcome summary, ``success`` marks validation/target
failures so the audit trail is complete even for rejected interventions.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _new_command_id() -> str:
    return f"cmd_{uuid.uuid4().hex[:12]}"


class GodAction(Base):
    """One god intervention: audit trail with parameters, result and success."""

    __tablename__ = "god_actions"
    __table_args__ = (Index("ix_god_actions_world_created", "world_id", "created_at"),)

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_command_id)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameters_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # World time (game minutes) the intervention was applied at.
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GodAction(cmd={self.command_id!r}, type={self.command_type!r}, success={self.success})"
