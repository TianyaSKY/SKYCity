"""StockService: cooperative-share subscription and redemption rule gate.

The stable ``stock_*`` IDs and API actions remain implementation contracts.
Residents see fixed-price cooperative shares: operating events update only a
transparent daily volume; they never change price or distribute dividends.
Every issued share has an active operating-company issuer, subscriptions are
limited by both total issuance and per-resident holding caps, and redemption
is funded by the same issuer at the fixed unit price.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import COOPERATIVE_SHARE_HOLDING_CAP
from app.database.models.agents import Agent
from app.database.models.companies import Company, CompanyTransaction
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
from app.services.seed_loader import load_companies, load_stocks
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_STOCK_MISSING = "合作社份额不存在"
MSG_NOT_ENOUGH_SHARES = "持有份额不足"
MSG_ISSUANCE_EXHAUSTED = "可认购份额不足"
MSG_UNBACKED_STOCK = "合作社份额没有有效发行经营单元"
MSG_HOLDING_CAP = (
    f"每位居民每种合作社份额最多持有 {COOPERATIVE_SHARE_HOLDING_CAP} 份"
)
MSG_INVALID_SHARE_QUANTITY = "份额数量必须为正整数"


class StockService:
    """Owns the stock rule gate for all worlds (one instance, like the
    EconomyService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._uow = UnitOfWork(session_factory)

    # ------------------------------------------------------------------ #
    # Seeding (per world)
    def seed(self, session: Session, world_id: str) -> None:
        """Seed only shares backed by an active operating company.

        A bad seed is a configuration error, not a partially listed public
        asset. Failing the world creation preserves the issuer invariant.
        """
        session.flush()
        stock_seeds = list(load_stocks(self.engine.world_data_dir))
        company_seeds = {
            str(seed["company_id"]): seed
            for seed in load_companies(self.engine.world_data_dir)
        }
        for stock_seed in stock_seeds:
            issuer_id = str(stock_seed.get("issuer_company_id") or "")
            company_seed = company_seeds.get(issuer_id)
            if not issuer_id or company_seed is None:
                raise ValueError(
                    f"合作社份额 {stock_seed.get('stock_id')} 没有有效发行企业"
                )
            if session.get(
                    Company, {"world_id": world_id, "company_id": issuer_id}
            ) is not None:
                continue
            initial_money = int(company_seed.get("initial_money") or 0)
            session.add(
                Company(
                    world_id=world_id,
                    company_id=issuer_id,
                    name=str(company_seed["name"]),
                    company_type=str(company_seed["company_type"]),
                    location_id=str(company_seed["location_id"]),
                    owner_agent_id=company_seed.get("owner_agent_id"),
                    manager_agent_id=company_seed.get("manager_agent_id"),
                    money=initial_money,
                    status="active",
                    founded_at=480,
                )
            )
            session.add(
                CompanyTransaction(
                    world_id=world_id,
                    company_id=issuer_id,
                    type="initial_capital",
                    amount=initial_money,
                    balance_after=initial_money,
                    reference_type="company",
                    reference_id=issuer_id,
                    reason="合作社经营单元初始资金",
                    world_time=480,
                )
            )
        session.flush()
        for seed in stock_seeds:
            issuer_id = str(seed.get("issuer_company_id") or "")
            issuer = session.get(
                Company, {"world_id": world_id, "company_id": issuer_id}
            )
            if issuer is None or issuer.status != "active":
                raise ValueError(
                    f"合作社份额 {seed.get('stock_id')} 没有有效发行企业"
                )
            unit_price = int(seed["base_price"])
            session.add(
                Stock(
                    world_id=world_id,
                    stock_id=seed["stock_id"],
                    name=seed["name"],
                    company_id=seed["company_id"],
                    issuer_company_id=issuer_id,
                    source=seed["source"],
                    base_price=unit_price,
                    price=unit_price,
                    outstanding_shares=int(seed["outstanding_shares"]),
                    day_business=0,
                )
            )

    # ------------------------------------------------------------------ #
    # Business events -> transparent operating volume
    # ------------------------------------------------------------------ #

    def on_event(self, session: Session, envelope: Any) -> None:
        """Increment a share's transparent daily operating volume at fixed price."""
        world_id = envelope.world_id
        payload = envelope.payload or {}
        stock: Stock | None = None
        if envelope.type == "item_purchased":
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
        elif envelope.type == "company_production_completed":
            stock = session.scalars(
                select(Stock).where(
                    Stock.world_id == world_id,
                    Stock.issuer_company_id == payload.get("company_id"),
                )
            ).first()
        if stock is None:
            return
        stock.day_business += 1
        runtime = self.engine.get_runtime(world_id)
        if runtime is not None:
            runtime.event_bus.publish(
                session,
                envelope.world_time,
                "stock_volume_changed",
                {
                    "stock_id": stock.stock_id,
                    "stock_name": stock.name,
                    "operating_volume": stock.day_business,
                },
                envelope.trace_id,
            )

    def reset_daily_operating_volume(
            self,
            session: Session,
            runtime: Any,
            world: World,
            world_time: int,
    ) -> None:
        """Reset the displayed daily operating volume without any payout."""
        stocks = session.scalars(
            select(Stock).where(Stock.world_id == world.world_id).order_by(Stock.stock_id)
        ).all()
        for stock in stocks:
            if stock.day_business <= 0:
                continue
            stock.day_business = 0
            runtime.event_bus.publish(
                session,
                world_time,
                "stock_volume_changed",
                {
                    "stock_id": stock.stock_id,
                    "stock_name": stock.name,
                    "operating_volume": 0,
                },
            )

    # ------------------------------------------------------------------ #
    # Trading (instant, idle-only, no credit, fixed unit price)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _issuer(
            session: Session, world: World, stock: Stock
    ) -> Company | None:
        """Return the active operating company that funds this share."""
        if not stock.issuer_company_id:
            return None
        return session.get(
            Company,
            {"world_id": world.world_id, "company_id": stock.issuer_company_id},
        )

    def buy_stock(
            self,
            world_id: str,
            agent_id: str,
            stock_id: str,
            shares: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Subscribe to ``shares`` of a cooperative share at its fixed unit price."""

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
            issuer = self._issuer(session, world, stock)
            if issuer is None:
                return False, None, MSG_UNBACKED_STOCK
            try:
                quantity = int(shares)
            except (TypeError, ValueError):
                return False, None, MSG_INVALID_SHARE_QUANTITY
            if quantity < 1:
                return False, None, MSG_INVALID_SHARE_QUANTITY
            holding = session.get(
                StockHolding,
                {"world_id": world_id, "agent_id": agent_id, "stock_id": stock_id},
            )
            held_shares = holding.shares if holding is not None else 0
            if held_shares + quantity > COOPERATIVE_SHARE_HOLDING_CAP:
                return False, None, MSG_HOLDING_CAP
            issued = session.scalar(
                select(func.coalesce(func.sum(StockHolding.shares), 0)).where(
                    StockHolding.world_id == world_id,
                    StockHolding.stock_id == stock_id,
                )
            ) or 0
            if issued + quantity > stock.outstanding_shares:
                return False, None, MSG_ISSUANCE_EXHAUSTED
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

            if holding is None:
                session.add(
                    StockHolding(
                        world_id=world_id,
                        agent_id=agent_id,
                        stock_id=stock_id,
                        shares=quantity,
                    )
                )
            else:
                holding.shares += quantity
            agent.money -= cost  # keep the in-memory agent consistent
            # Subscription proceeds fund the issuing cooperative but never
            # count as operating surplus for the leader stipend.
            issuer.money += cost
            session.add(
                CompanyTransaction(
                    world_id=world_id,
                    company_id=issuer.company_id,
                    type="share_issue",
                    amount=cost,
                    balance_after=issuer.money,
                    related_agent_id=agent_id,
                    quantity=quantity,
                    reference_type="cooperative_share",
                    reference_id=stock_id,
                    reason=f"认购合作社份额 {stock.name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="coop_share_buy",
                    amount=-cost,
                    balance_after=agent.money,
                    item_id=stock_id,
                    quantity=quantity,
                    reason=f"认购合作社份额 {stock.name}×{quantity}",
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
                    "reason": f"认购合作社份额 {stock.name}×{quantity}",
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
        """Redeem cooperative shares at their issuer-backed fixed unit price."""

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
                return False, None, MSG_BUSY
            stock = session.get(Stock, {"world_id": world_id, "stock_id": stock_id})
            if stock is None:
                return False, None, MSG_STOCK_MISSING
            issuer = self._issuer(session, world, stock)
            if issuer is None:
                return False, None, MSG_UNBACKED_STOCK
            try:
                quantity = int(shares)
            except (TypeError, ValueError):
                return False, None, MSG_INVALID_SHARE_QUANTITY
            if quantity < 1:
                return False, None, MSG_INVALID_SHARE_QUANTITY
            proceeds = quantity * stock.price
            if issuer.money < proceeds:
                return False, None, "发行合作社资金不足，无法回购份额"
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
                session.refresh(holding)
            if holding is not None and holding.shares <= 0:
                session.delete(holding)
            issuer.money -= proceeds
            session.add(
                CompanyTransaction(
                    world_id=world_id,
                    company_id=issuer.company_id,
                    type="share_buyback",
                    amount=-proceeds,
                    balance_after=issuer.money,
                    related_agent_id=agent_id,
                    quantity=quantity,
                    reference_type="cooperative_share",
                    reference_id=stock_id,
                    reason=f"回购合作社份额 {stock.name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            agent.money += proceeds
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="coop_share_sell",
                    amount=proceeds,
                    balance_after=agent.money,
                    item_id=stock_id,
                    quantity=quantity,
                    reason=f"退出合作社份额 {stock.name}×{quantity}",
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
                    "reason": f"退出合作社份额 {stock.name}×{quantity}",
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Listing (read-only, full world state)
    # ------------------------------------------------------------------ #

    def list_stocks(self, world_id: str) -> dict[str, Any] | None:
        """All cooperative-share data and holdings for one live world."""
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
            issued_by_stock: dict[str, int] = {}
            for holding in holdings:
                issued_by_stock[holding.stock_id] = (
                    issued_by_stock.get(holding.stock_id, 0) + holding.shares
                )
            return {
                "stocks": [
                    {
                        "stock_id": stock.stock_id,
                        "name": stock.name,
                        "unit_price": stock.price,
                        "operating_volume": stock.day_business,
                        "source": stock.source,
                        "company_id": stock.company_id,
                        "issuer_company_id": stock.issuer_company_id,
                        "outstanding_shares": stock.outstanding_shares,
                        "available_shares": max(
                            stock.outstanding_shares
                            - issued_by_stock.get(stock.stock_id, 0),
                            0,
                        ),
                        "holding_cap": COOPERATIVE_SHARE_HOLDING_CAP,
                    }
                    for stock in stocks
                ],
                "holdings": [
                    {
                        "agent_id": holding.agent_id,
                        "stock_id": holding.stock_id,
                        "shares": holding.shares,
                    }
                    for holding in holdings
                ],
            }
        finally:
            session.close()
