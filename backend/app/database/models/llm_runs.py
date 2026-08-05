"""LLM run records: one row per agent decision (docs/agent-prompt.md §6).

Auditable trace of every LLM decision: what the model picked, whether the
world accepted it, and how long/expensive the call was. Reasoning output is
never stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LLMRun(Base):
    """One decision cycle for one agent (provider call + tool execution)."""

    __tablename__ = "llm_runs"
    __table_args__ = (Index("ix_llm_runs_world_agent", "world_id", "agent_id"),)

    run_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_new_run_id)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    world_time: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_name: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tool_arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tool_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    success: Mapped[bool] = mapped_column(Integer, nullable=False, default=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    raw_summary: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def to_dict(self) -> dict:
        """API shape for GET .../decisions (created_at ISO string)."""
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "world_id": self.world_id,
            "world_time": self.world_time,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "tool_name": self.tool_name,
            "tool_arguments": self.tool_arguments,
            "tool_result": self.tool_result,
            "success": bool(self.success),
            "error_type": self.error_type,
            "trace_id": self.trace_id,
            "raw_summary": self.raw_summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LLMRun(run_id={self.run_id!r}, agent={self.agent_id!r}, "
            f"tool={self.tool_name!r}, success={self.success})"
        )
