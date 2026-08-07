"""Agent rows: identity card data plus live simulation state.

The agent's current action (move / wait) is stored denormalised on the row so
snapshots are a single query; pending completions live in scheduled_actions.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.config.gameplay import (
    INITIAL_ENERGY,
    INITIAL_LONELINESS,
    INITIAL_MONEY,
    INITIAL_MOOD,
    INITIAL_SATIETY,
)
from app.database.session import Base


class Agent(Base):
    """An agent seeded from an identity card at a spawn point."""

    __tablename__ = "agents"
    # agent ids (agent_linxia, ...) are shared across worlds -> composite PK.
    __table_args__ = ()

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Identity card (world_data/identities/*.json).
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    occupation: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    background: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    values: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    long_term_goals: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    speaking_style: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    personality: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Simulation state.
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="down")
    # Soft reference to locations(world_id, location_id); not an FK so agents may
    # stand outside any location (spawn, moving between places).
    location_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    satiety: Mapped[int] = mapped_column(Integer, nullable=False, default=INITIAL_SATIETY)
    energy: Mapped[int] = mapped_column(Integer, nullable=False, default=INITIAL_ENERGY)
    mood: Mapped[int] = mapped_column(Integer, nullable=False, default=INITIAL_MOOD)
    loneliness: Mapped[int] = mapped_column(Integer, nullable=False, default=INITIAL_LONELINESS)
    money: Mapped[int] = mapped_column(Integer, nullable=False, default=INITIAL_MONEY)

    # Current action (R1: at most one in flight). None = idle.
    action_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action_started_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_ends_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # LLM decision loop (M3): in-flight guard + consecutive failure counter.
    is_deciding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # M8: stability / cost control observability.
    last_decision_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_token_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Agent(world_id={self.world_id!r}, agent_id={self.agent_id!r}, at=({self.col},{self.row}))"
