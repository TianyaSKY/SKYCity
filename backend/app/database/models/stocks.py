"""Stock + stock holding rows: the town stock market (M10).

``Stock.price`` is the live quote (business events +1, hourly deterministic
noise, god overrides); ``prev_price`` is the previous close (snapshot at the
daily dividend boundary), used for the 涨跌 display. ``day_business`` counts
today's business events; dividends are paid from it at 00:00.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Stock(Base):
    """One listed town company (store or job), with live quote state."""

    __tablename__ = "stocks"

    world_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    stock_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)  # store_id 或 job_id
    # The real Company whose treasury backs this listing (A2): buy credits it,
    # sell/dividends debit it. NULL -> backed by the village treasury instead.
    issuer_company_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # "store" | "job"
    base_price: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # 现价, 下限 1
    prev_price: Mapped[int] = mapped_column(Integer, nullable=False)  # 昨收(日界快照)
    outstanding_shares: Mapped[int] = mapped_column(Integer, nullable=False)  # 仅信息展示
    day_business: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 当日经营事件数
    last_div_per_share: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Stock(world_id={self.world_id!r}, stock_id={self.stock_id!r}, price={self.price})"


class StockHolding(Base):
    """Shares of one stock held by one agent (composite per-world PK)."""

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
    # 持仓均价（金币/股）：买入时按加权平均更新，卖出不变；
    # 观察文本据此给出浮盈/浮亏，供智能体判断止盈止损。
    avg_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StockHolding(agent={self.agent_id!r}, stock={self.stock_id!r}, shares={self.shares})"
