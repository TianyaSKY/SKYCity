"""StockService: the stock market rule gate (M10).

World rules enforced here (docs/world-rules.md R18): R18.1 trading is an
instant action requiring the agent to be idle (R1), no credit (R7), no
location requirement, and trades never move the price; R18.2 the price moves
with business events (store sales / completed work, +1 each, floor 1) plus a
deterministic hourly noise in [-2, +2]; R18.3 dividends are paid at 00:00
from the day's business count; R18.4 the god command can set any price.

Buy/sell run inside the same retrying BEGIN IMMEDIATE transaction as the
economy service; the conditional UPDATE is the atomic guard (a concurrent
sell cannot oversell a holding). Every accepted action publishes its event
envelopes and returns ``(ok, envelope, reason)`` — the action-service shape.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import (
    DIV_BUSINESS_PER_SHARE,
    MAX_SHARES,
    STOCK_NOISE_RANGE,
)
from app.database.models.agents import Agent
from app.database.models.stocks import Stock, StockHolding
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.services.economy_service import (
    MSG_AGENT_MISSING,
    MSG_BUSY,
    MSG_NO_MONEY,
    MSG_PAUSED,
    MSG_WORLD_MISSING,
)
from app.services.seed_loader import load_stocks
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_STOCK_MISSING = "股票不存在"
MSG_NOT_ENOUGH_SHARES = "持股不足"


def _hourly_noise(world_id: str, stock_id: str, hour: int) -> int:
    """Deterministic pseudo-random noise in [-STOCK_NOISE_RANGE, +STOCK_NOISE_RANGE]
    for one (world, stock, hour).

    hashlib.md5 instead of built-in hash(): hash() is salted per process, so
    the value would not be stable across restarts / save-restore replay.
    """
    digest = hashlib.md5(f"{world_id}:{stock_id}:{hour}".encode()).hexdigest()
    return int(digest[:8], 16) % (2 * STOCK_NOISE_RANGE + 1) - STOCK_NOISE_RANGE


class StockService:
    """Owns the stock rule gate for all worlds (one instance, like the
    EconomyService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._uow = UnitOfWork(session_factory)

    # ------------------------------------------------------------------ #
    # Seeding (per world)
    # ------------------------------------------------------------------ #

    def seed(self, session: Session, world_id: str) -> None:
        """Insert the 3 listed stocks at their base price (M10)."""
        for seed in load_stocks(self.engine.world_data_dir):
            session.add(
                Stock(
                    world_id=world_id,
                    stock_id=seed["stock_id"],
                    name=seed["name"],
                    company_id=seed["company_id"],
                    source=seed["source"],
                    base_price=seed["base_price"],
                    price=seed["base_price"],
                    prev_price=seed["base_price"],
                    outstanding_shares=seed["outstanding_shares"],
                    day_business=0,
                    last_div_per_share=0,
                )
            )

    # ------------------------------------------------------------------ #
    # Business events -> price (R18.2, part 1)
    # ------------------------------------------------------------------ #

    def on_event(self, session: Session, envelope: Any) -> None:
        """R18.2: each business event nudges its company's stock +1 (floor 1).

        Runs inside the publisher's transaction (the price bump commits with
        the source event; no separate event is published here — the hourly
        tick broadcasts the aggregated quote).
        """
        world_id = envelope.world_id
        payload = envelope.payload or {}
        stock: Stock | None = None
        if envelope.type == "item_purchased":
            # M18: the payload carries the selling store, so a personal shop
            # next to the village store credits the right listing. The old
            # first-product path stays as a fallback for legacy events and
            # directly-published test envelopes without store_id.
            store_id = payload.get("store_id")
            if store_id:
                stock = session.scalars(
                    select(Stock).where(
                        Stock.world_id == world_id,
                        Stock.source == "store",
                        Stock.company_id == store_id,
                    )
                ).first()
            if stock is None:
                product = session.scalars(
                    select(StoreProduct).where(
                        StoreProduct.world_id == world_id,
                        StoreProduct.item_id == payload.get("item_id"),
                    )
                ).first()
                if product is not None:
                    stock = session.scalars(
                        select(Stock).where(
                            Stock.world_id == world_id,
                            Stock.source == "store",
                            Stock.company_id == product.store_id,
                        )
                    ).first()
        elif envelope.type == "work_completed":
            stock = session.scalars(
                select(Stock).where(
                    Stock.world_id == world_id,
                    Stock.source == "job",
                    Stock.company_id == payload.get("job_id"),
                )
            ).first()
        if stock is None:
            return
        stock.day_business += 1
        stock.price = max(1, stock.price + 1)

    # ------------------------------------------------------------------ #
    # Hourly tick (R18.2, part 2) + daily dividends (R18.3)
    # ------------------------------------------------------------------ #

    def tick_prices(
            self,
            session: Session,
            runtime: Any,
            world: World,
            world_time: int,
    ) -> None:
        """Hourly: deterministic noise on every stock + one quote event each.

        Every stock publishes exactly one ``stock_price_changed`` per hour
        (the frontend silently drops zero-delta lines) so the panel always
        refreshes ``day_business`` even when the price did not move.
        """
        stocks = session.scalars(
            select(Stock).where(Stock.world_id == world.world_id).order_by(Stock.stock_id)
        ).all()
        hour = world_time // 60
        for stock in stocks:
            stock.price = max(1, stock.price + _hourly_noise(world.world_id, stock.stock_id, hour))
            runtime.event_bus.publish(
                session,
                world_time,
                "stock_price_changed",
                {
                    "stock_id": stock.stock_id,
                    "stock_name": stock.name,
                    "price": stock.price,
                    "prev_price": stock.prev_price,
                    "day_business": stock.day_business,
                },
            )

    def pay_dividends(
            self,
            session: Session,
            runtime: Any,
            world: World,
            world_time: int,
    ) -> None:
        """R18.3: at 00:00 pay out the day's profit as dividends.

        div_per_share = max(1, day_business // 3) when the company had any
        business today (0 otherwise — no event). prev_price becomes the close,
        day_business resets. Each payout is a ``dividend`` transaction plus a
        per-agent ``money_changed`` event.
        """
        stocks = session.scalars(
            select(Stock).where(Stock.world_id == world.world_id).order_by(Stock.stock_id)
        ).all()
        for stock in stocks:
            div = max(1, stock.day_business // DIV_BUSINESS_PER_SHARE) if stock.day_business > 0 else 0
            stock.last_div_per_share = div
            stock.prev_price = stock.price  # close
            stock.day_business = 0
            if div <= 0:
                continue
            holdings = session.scalars(
                select(StockHolding).where(
                    StockHolding.world_id == world.world_id,
                    StockHolding.stock_id == stock.stock_id,
                )
            ).all()
            payouts: list[dict[str, Any]] = []
            for holding in holdings:
                if holding.shares <= 0:
                    continue
                agent = session.get(
                    Agent,
                    {"world_id": world.world_id, "agent_id": holding.agent_id},
                )
                if agent is None:
                    continue
                amount = holding.shares * div
                agent.money += amount
                session.add(
                    Transaction(
                        world_id=world.world_id,
                        agent_id=agent.agent_id,
                        type="dividend",
                        amount=amount,
                        balance_after=agent.money,
                        item_id=stock.stock_id,
                        quantity=holding.shares,
                        reason=f"股票 {stock.name} 每日分红",
                        world_time=world_time,
                        trace_id="",
                    )
                )
                payouts.append(
                    {"agent_id": agent.agent_id, "shares": holding.shares, "amount": amount}
                )
            if not payouts:
                continue
            runtime.event_bus.publish(
                session,
                world_time,
                "dividend_paid",
                {
                    "stock_id": stock.stock_id,
                    "stock_name": stock.name,
                    "div_per_share": div,
                    "payouts": payouts,
                },
            )
            for payout in payouts:
                agent = session.get(
                    Agent,
                    {"world_id": world.world_id, "agent_id": payout["agent_id"]},
                )
                if agent is None:
                    continue
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "money_changed",
                    {
                        "agent_id": agent.agent_id,
                        "amount": payout["amount"],
                        "balance": agent.money,
                        "reason": f"股票 {stock.name} 每日分红",
                    },
                )

    # ------------------------------------------------------------------ #
    # Trading (R18.1: instant, idle-only, no credit, no price impact)
    # ------------------------------------------------------------------ #

    def buy_stock(
            self,
            world_id: str,
            agent_id: str,
            stock_id: str,
            shares: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Buy ``shares`` of ``stock_id`` at the current price (R7: no credit)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY  # R1: trading requires idle
            stock = session.get(Stock, {"world_id": world_id, "stock_id": stock_id})
            if stock is None:
                return False, None, MSG_STOCK_MISSING

            quantity = max(1, min(int(shares), MAX_SHARES))
            cost = quantity * stock.price
            if agent.money < cost:
                return False, None, MSG_NO_MONEY  # R7: no credit
            result = session.execute(
                update(Agent)
                .where(
                    Agent.world_id == world_id,
                    Agent.agent_id == agent_id,
                    Agent.money >= cost,
                )
                .values(money=Agent.money - cost)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                return False, None, MSG_NO_MONEY  # lost a concurrent race

            holding = session.get(
                StockHolding,
                {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
            )
            if holding is None:
                session.add(
                    StockHolding(
                        world_id=world_id,
                        agent_id=agent_id,
                        stock_id=stock_id,
                        shares=quantity,
                        avg_cost=stock.price,
                    )
                )
            else:
                # Weighted-average cost basis; sells never change it, so the
                # remaining shares keep the same 均价 (float is rounded).
                total = holding.shares + quantity
                holding.avg_cost = round(
                    (holding.avg_cost * holding.shares + stock.price * quantity) / total
                )
                holding.shares = total
            agent.money -= cost  # keep the in-memory agent consistent
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="stock_buy",
                    amount=-cost,
                    balance_after=agent.money,
                    item_id=stock_id,
                    quantity=quantity,
                    reason=f"买入 {stock.name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "stock_bought",
                {
                    "agent_id": agent_id,
                    "stock_id": stock_id,
                    "stock_name": stock.name,
                    "shares": quantity,
                    "unit_price": stock.price,
                    "total": cost,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": agent_id,
                    "amount": -cost,
                    "balance": agent.money,
                    "reason": f"买入 {stock.name}×{quantity}",
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    def sell_stock(
            self,
            world_id: str,
            agent_id: str,
            stock_id: str,
            shares: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Sell ``shares`` of ``stock_id`` at the current price (no oversell)."""

        def _inner(session: Session) -> tuple[bool, Any, str | None]:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, None, MSG_WORLD_MISSING
            world = session.get(World, world_id)
            if world is None:
                return False, None, MSG_WORLD_MISSING
            if world.paused:
                return False, None, MSG_PAUSED
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return False, None, MSG_AGENT_MISSING
            if agent.action_type is not None:
                return False, None, MSG_BUSY  # R1
            stock = session.get(Stock, {"world_id": world_id, "stock_id": stock_id})
            if stock is None:
                return False, None, MSG_STOCK_MISSING

            quantity = max(1, min(int(shares), MAX_SHARES))
            result = session.execute(
                update(StockHolding)
                .where(
                    StockHolding.world_id == world_id,
                    StockHolding.agent_id == agent_id,
                    StockHolding.stock_id == stock_id,
                    StockHolding.shares >= quantity,
                )
                .values(shares=StockHolding.shares - quantity)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                return False, None, MSG_NOT_ENOUGH_SHARES

            holding = session.get(
                StockHolding,
                {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
            )
            if holding is not None:
                session.refresh(holding)  # see the post-UPDATE share count
            if holding is not None and holding.shares <= 0:
                session.delete(holding)
            proceeds = quantity * stock.price
            agent.money += proceeds
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="stock_sell",
                    amount=proceeds,
                    balance_after=agent.money,
                    item_id=stock_id,
                    quantity=quantity,
                    reason=f"卖出 {stock.name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "stock_sold",
                {
                    "agent_id": agent_id,
                    "stock_id": stock_id,
                    "stock_name": stock.name,
                    "shares": quantity,
                    "unit_price": stock.price,
                    "total": proceeds,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": agent_id,
                    "amount": proceeds,
                    "balance": agent.money,
                    "reason": f"卖出 {stock.name}×{quantity}",
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Listing (read-only, full world state)
    # ------------------------------------------------------------------ #

    def list_stocks(self, world_id: str) -> dict[str, Any] | None:
        """All quotes + every holding of one world; None when the world is missing."""
        if self.engine.get_runtime(world_id) is None:
            return None
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                return None
            stocks = session.scalars(
                select(Stock).where(Stock.world_id == world_id).order_by(Stock.stock_id)
            ).all()
            holdings = session.scalars(
                select(StockHolding)
                .where(StockHolding.world_id == world_id)
                .order_by(StockHolding.agent_id, StockHolding.stock_id)
            ).all()
            return {
                "stocks": [
                    {
                        "stock_id": stock.stock_id,
                        "name": stock.name,
                        "price": stock.price,
                        "prev_price": stock.prev_price,
                        "day_business": stock.day_business,
                        "last_div_per_share": stock.last_div_per_share,
                        "source": stock.source,
                        "company_id": stock.company_id,
                    }
                    for stock in stocks
                ],
                "holdings": [
                    {
                        "agent_id": holding.agent_id,
                        "stock_id": holding.stock_id,
                        "shares": holding.shares,
                        "avg_cost": holding.avg_cost,
                    }
                    for holding in holdings
                ],
            }
        finally:
            session.close()
