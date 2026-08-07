"""Job + employment rows: work definitions and completed-work history (M5)."""

from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Job(Base):
    """A work offer at a location (seeded from world_data/jobs/jobs.json)."""

    __tablename__ = "jobs"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Soft references: the interactable the job is anchored to (map object id).
    location_id: Mapped[str] = mapped_column(String(64), nullable=False)
    interactable_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    wage: Mapped[int] = mapped_column(Integer, nullable=False)
    energy_cost_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Job(world_id={self.world_id!r}, job_id={self.job_id!r})"


class WorkHistory(Base):
    """Cumulative casual-work history for one (agent, job) pair (R10).

    Renamed from ``Employment`` (M13, R22): this is NOT a formal labour
    contract — formal jobs live in ``employment_contracts``. The table name
    ``employments`` is kept for backward compatibility with existing DBs and
    save files.
    """

    __tablename__ = "employments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "agent_id"],
            ["agents.world_id", "agents.agent_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["world_id", "job_id"],
            ["jobs.world_id", "jobs.job_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hours_worked: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WorkHistory(world_id={self.world_id!r}, agent={self.agent_id!r}, job={self.job_id!r})"
