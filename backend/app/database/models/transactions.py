"""Transaction rows: the auditable money ledger of the economy (R7/R10).

One row per money movement; ``amount`` is signed (negative = spent). The
world_time/trace_id pair ties a transaction back to the event stream.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def _new_tx_id() -> str:
    return f"tx_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Transaction(Base):
    """One signed money movement for one agent."""

    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_world_agent", "world_id", "agent_id"),)

    tx_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_new_tx_id)
    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # expense | income | work_wage | refund
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    world_time: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Transaction(tx={self.tx_id!r}, type={self.type!r}, amount={self.amount})"
