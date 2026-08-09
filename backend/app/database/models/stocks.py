"""Cooperative share issuance and resident holding rows.

``Stock.price`` is the fixed subscription and redemption unit price.  It
changes only through the administrator's explicit unit-price command;
``day_business`` is transparent daily operating volume, reset at day end.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Stock(Base):
    """One cooperative operating unit's fixed-price share issue."""

    __tablename__ = "stocks"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    stock_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)  # store_id 或 job_id
    # The operating cooperative whose account accepts subscriptions and funds
    # redemptions. Every seeded issue must name an active company.
    issuer_company_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # "store" | "job"
    base_price: Mapped[int] = mapped_column(Integer, nullable=False)
    # Kept as the storage/API-compatible field name; this is a fixed unit price.
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    outstanding_shares: Mapped[int] = mapped_column(Integer, nullable=False)
    day_business: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # 当日透明经营量

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Stock(world_id={self.world_id!r}, stock_id={self.stock_id!r}, price={self.price})"


class StockHolding(Base):
    """Cooperative-share units held by one resident (composite per-world PK)."""

    __tablename__ = "stock_holdings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["world_id", "agent_id"],
            ["agents.world_id", "agents.agent_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["world_id", "stock_id"],
            ["stocks.world_id", "stocks.stock_id"],
            ondelete="CASCADE",
        ),
    )

    world_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stock_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    shares: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StockHolding(agent={self.agent_id!r}, stock={self.stock_id!r}, shares={self.shares})"
