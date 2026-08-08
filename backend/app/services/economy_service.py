"""EconomyService: the economy rule gate — work / buy / sell / use (M5).

World rules enforced here (docs/world-rules.md): R1 (one action; move 独占),
R3 (work uninterruptible — no other tool may run mid-work), R4 (last-item
race via BEGIN IMMEDIATE + conditional UPDATE), R7 (no credit), R8 (store
hours), R10 (wage + products settled at completion), R11 (satiety=0 blocks
work), R12 (energy=0 blocks work; forced rest lives in the decision service),
R14 (work drains energy by the job's intensity).

Every accepted action publishes its event envelopes through the runtime event
bus and returns ``(ok, envelope, reason)`` — the same shape the action
service uses, so tools and the HTTP layer stay uniform. Buy/sell run inside a
retrying BEGIN IMMEDIATE transaction (app.database.unit_of_work).
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.companies import Company, CompanyTransaction
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Job, WorkHistory
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.services.seed_loader import load_jobs
from app.world_engine.engine import WorldEngine, is_location_open

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_WORLD_MISSING = "世界不存在"
MSG_PAUSED = "世界已暂停"
MSG_AGENT_MISSING = "智能体不存在"
MSG_BUSY = "当前行动未完成"
MSG_JOB_MISSING = "工作不存在"
MSG_FORMAL_ONLY = "该工作仅限正式员工班次"
MSG_NOT_AT_JOB = "不在工作地点"
MSG_LOCATION_CLOSED = "地点未开门"
MSG_SATIETY_EMPTY = "饱食度耗尽，无法工作"
MSG_EXHAUSTED = "精力耗尽，无法工作"
MSG_PRODUCT_MISSING = "商店没有该商品"
MSG_NOT_AT_STORE = "不在商店"
MSG_STORE_CLOSED = "商店未开门"
MSG_STORE_UNBOUND = "商店未绑定企业，无法交易"
MSG_NO_MONEY = "余额不足"
MSG_NO_STOCK = "库存不足"
MSG_NOT_IN_INVENTORY = "背包中没有该物品"
MSG_NOT_BUYABLE = "商店不收购该物品"
MSG_SELL_TO_SELF = "不能卖货给自己店铺"
MSG_OWNER_POOR = "店主资金不足"
MSG_STORE_FULL = "商店收不下"
MSG_ITEM_MISSING = "物品不存在"
MSG_NOT_FOOD = "该物品不是食物"


class EconomyService:
    """Owns the economy rule gate for all worlds (one instance, like the
    ActionExecutionService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._uow = UnitOfWork(session_factory)

    # ------------------------------------------------------------------ #
    # Work (R10: settle wage + products at completion)
    # ------------------------------------------------------------------ #

    def work_start(
            self,
            world_id: str,
            agent_id: str,
            job_id: str,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Validate + start a work action (R1/R3/R8/R11/R12)."""
        session = self._session_factory()
        try:
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
                return False, None, MSG_BUSY  # R1/R3: work is exclusive
            job = session.get(Job, {"world_id": world_id, "job_id": job_id})
            if job is None:
                return False, None, MSG_JOB_MISSING
            # M16: formal-only jobs (production recipes) reject the casual
            # work() path — they run exclusively as formal shifts.
            if any(
                seed["job_id"] == job_id and seed.get("formal_only")
                for seed in load_jobs()
            ):
                return False, None, MSG_FORMAL_ONLY
            if agent.location_id != job.location_id:
                return False, None, MSG_NOT_AT_JOB
            location = session.get(
                WorldLocation, {"world_id": world_id, "location_id": job.location_id}
            )
            if location is not None and not is_location_open(
                    location.location_type, location.open_hour, location.close_hour, world.world_time
            ):
                return False, None, MSG_LOCATION_CLOSED  # R8
            if agent.satiety <= 0:
                return False, None, MSG_SATIETY_EMPTY  # R11
            if agent.energy <= 0:
                return False, None, MSG_EXHAUSTED  # R12

            ends_at = world.world_time + job.duration_minutes
            agent.action_type = "work"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {"job_id": job_id, "reason": reason}
            runtime.scheduler.schedule(
                session,
                agent_id,
                "work_completed",
                ends_at,
                {"job_id": job_id, "reason": reason, "trace_id": trace_id},
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "work_started",
                {
                    "agent_id": agent_id,
                    "job_id": job_id,
                    "job_name": job.name,
                    "duration_minutes": job.duration_minutes,
                    "ends_at": ends_at,
                    "reason": reason,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    def handle_work_completed(self, session: Session, action: ScheduledAction) -> None:
        """Scheduler handler for "work_completed": settle wage + products.

        R10: one-shot settlement at the due world_time — energy drained by the
        job's intensity (R14), wage credited, products into the inventory,
        employment history updated, and a work_wage transaction recorded.
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "work":
            return  # stale or already replaced
        payload = action.payload or {}
        job_id = payload.get("job_id")
        job = session.get(Job, {"world_id": action.world_id, "job_id": job_id})
        trace_id = payload.get("trace_id")
        if job is None:
            self.engine.action_service._clear_action(agent)
            return
        world_time = runtime.clock.world_time

        # M12 C4: held tools/inputs boost wage and yield (sum by held
        # quantity across the agent's inventory; no held items -> 0 bonus).
        items = {
            item.item_id: item
            for item in session.scalars(
                select(Item).where(Item.world_id == action.world_id)
            ).all()
        }
        bonus_pct = 0
        yield_extra = 0
        inventory_rows = session.scalars(
            select(Inventory).where(
                Inventory.world_id == action.world_id,
                Inventory.agent_id == action.agent_id,
            )
        ).all()
        for row in inventory_rows:
            item = items.get(row.item_id)
            if item is None:
                continue
            bonus_pct += self._item_work_bonus(item, job_id) * row.quantity
            yield_extra += item.yield_bonus * row.quantity

        energy_spent = max(int(job.energy_cost_per_hour * job.duration_minutes / 60), 0)
        agent.energy = max(0, agent.energy - energy_spent)  # R14
        wage = job.wage * (100 + bonus_pct) // 100  # R10 + M12 work bonus
        agent.money += wage

        produced: list[dict[str, Any]] = []
        for product in job.products_json or []:
            item_id = str(product.get("item_id") or "")
            quantity = int(product.get("quantity") or 0) + yield_extra
            if not item_id or quantity <= 0:
                continue
            if session.get(Item, {"world_id": action.world_id, "item_id": item_id}) is None:
                continue  # products only materialise for known items
            self._add_inventory(session, action.world_id, action.agent_id, item_id, quantity)
            produced.append({"item_id": item_id, "quantity": quantity})

        employment = session.get(
            WorkHistory,
            {"world_id": action.world_id, "agent_id": action.agent_id, "job_id": job_id},
        )
        if employment is None:
            employment = WorkHistory(
                world_id=action.world_id,
                agent_id=action.agent_id,
                job_id=job_id,
                hours_worked=0.0,
                total_earned=0,
            )
            session.add(employment)
        employment.hours_worked += job.duration_minutes / 60.0
        employment.total_earned += wage

        session.add(
            Transaction(
                world_id=action.world_id,
                agent_id=action.agent_id,
                type="work_wage",
                amount=wage,
                balance_after=agent.money,
                item_id=None,
                quantity=None,
                reason=f"完成工作 {job.name} 获得工资",
                world_time=world_time,
                trace_id=trace_id or "",
            )
        )

        runtime.event_bus.publish(
            session,
            world_time,
            "work_completed",
            {
                "agent_id": action.agent_id,
                "job_id": job_id,
                "job_name": job.name,
                "wage": wage,
                "products": produced,
                "energy_spent": energy_spent,
            },
            trace_id,
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "money_changed",
            {
                "agent_id": action.agent_id,
                "amount": wage,
                "balance": agent.money,
                "reason": f"完成工作 {job.name} 获得工资",
            },
            trace_id,
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "inventory_changed",
            {
                "agent_id": action.agent_id,
                "items": self._inventory_list(session, action.world_id, action.agent_id),
            },
            trace_id,
        )
        self.engine.action_service._clear_action(agent)
        # M3: autonomous worlds re-arm the LLM loop now that work ended.
        self.engine.action_service._maybe_schedule_next_decision(session, action)

    # ------------------------------------------------------------------ #
    # Buy (R4 atomic guard, R7 no credit)
    # ------------------------------------------------------------------ #

    def buy(
            self,
            world_id: str,
            agent_id: str,
            item_id: str,
            quantity: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Buy ``quantity`` of ``item_id`` at the store covering the agent.

        Runs inside a retrying BEGIN IMMEDIATE transaction. The conditional
        ``UPDATE ... WHERE stock >= qty`` is the real R4 guard: exactly one
        concurrent buyer wins the last item.
        """

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
                return False, None, MSG_BUSY  # R1: move 独占, work exclusive
            found = self._find_product(session, world_id, item_id, agent.location_id)
            if found is None:
                return False, None, MSG_PRODUCT_MISSING
            product, store = found
            if agent.location_id != store.location_id:
                return False, None, MSG_NOT_AT_STORE
            if not self._store_open(session, world_id, store, world.world_time):
                return False, None, MSG_STORE_CLOSED  # R8
            if not store.company_id and not store.owner_agent_id:
                # A4: unbound stores would destroy money (agent pays, nobody
                # receives) — reject before any balance/stock mutation.
                return False, None, MSG_STORE_UNBOUND

            total = product.sell_price * quantity
            if agent.money < total:
                return False, None, MSG_NO_MONEY  # R7: no credit
            result = session.execute(
                update(StoreProduct)
                .where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store.store_id,
                    StoreProduct.item_id == item_id,
                    StoreProduct.stock >= quantity,
                )
                .values(stock=StoreProduct.stock - quantity)
            )
            if result.rowcount == 0:
                return False, None, MSG_NO_STOCK  # R4: lost the race

            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            agent.money -= total
            self._add_inventory(session, world_id, agent_id, item_id, quantity)
            # M13 R33: sale proceeds go to the owning company (same txn).
            company = (
                session.get(
                    Company,
                    {"world_id": world_id, "company_id": store.company_id},
                )
                if store.company_id
                else None
            )
            if company is not None:
                company.money += total
                session.add(
                    CompanyTransaction(
                        world_id=world_id,
                        company_id=company.company_id,
                        type="sale_income",
                        amount=total,
                        balance_after=company.money,
                        related_agent_id=agent_id,
                        related_item_id=item_id,
                        quantity=quantity,
                        reference_type="store",
                        reference_id=store.store_id,
                        reason=f"商店售出 {item_name}×{quantity}",
                        world_time=world.world_time,
                        trace_id=trace_id or "",
                    )
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "company_sale_completed",
                    {
                        "company_id": company.company_id,
                        "store_id": store.store_id,
                        "item_id": item_id,
                        "quantity": quantity,
                        "unit_price": product.sell_price,
                        "total": total,
                    },
                    trace_id,
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "company_money_changed",
                    {
                        "company_id": company.company_id,
                        "amount": total,
                        "balance": company.money,
                        "reason": f"商店售出 {item_name}×{quantity}",
                    },
                    trace_id,
                )
            elif store.owner_agent_id:
                # M18 R41: personal-store proceeds go straight to the owner's
                # balance (parallel path to R33's company settlement).
                owner = session.get(
                    Agent,
                    {"world_id": world_id, "agent_id": store.owner_agent_id},
                )
                if owner is not None:
                    owner.money += total
                    session.add(
                        Transaction(
                            world_id=world_id,
                            agent_id=owner.agent_id,
                            type="sale_income",
                            amount=total,
                            balance_after=owner.money,
                            item_id=item_id,
                            quantity=quantity,
                            reason=f"店铺售出 {item_name}×{quantity}",
                            world_time=world.world_time,
                            trace_id=trace_id or "",
                        )
                    )
                    runtime.event_bus.publish(
                        session,
                        world.world_time,
                        "store_sale_completed",
                        {
                            "store_id": store.store_id,
                            "owner_agent_id": owner.agent_id,
                            "buyer_agent_id": agent_id,
                            "item_id": item_id,
                            "item_name": item_name,
                            "quantity": quantity,
                            "unit_price": product.sell_price,
                            "total": total,
                        },
                        trace_id,
                    )
                    runtime.event_bus.publish(
                        session,
                        world.world_time,
                        "money_changed",
                        {
                            "agent_id": owner.agent_id,
                            "amount": total,
                            "balance": owner.money,
                            "reason": f"店铺售出 {item_name}×{quantity}",
                        },
                        trace_id,
                    )
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="expense",
                    amount=-total,
                    balance_after=agent.money,
                    item_id=item_id,
                    quantity=quantity,
                    reason=f"购买 {item_name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "item_purchased",
                {
                    "agent_id": agent_id,
                    "item_id": item_id,
                    "item_name": item_name,
                    "quantity": quantity,
                    "unit_price": product.sell_price,
                    "total": total,
                    # M18: the selling store — lets the stock hook credit the
                    # right listing when the same item sells at several shops.
                    "store_id": store.store_id,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": agent_id,
                    "amount": -total,
                    "balance": agent.money,
                    "reason": f"购买 {item_name}×{quantity}",
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "inventory_changed",
                {
                    "agent_id": agent_id,
                    "items": self._inventory_list(session, world_id, agent_id),
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Sell
    # ------------------------------------------------------------------ #

    def sell(
            self,
            world_id: str,
            agent_id: str,
            item_id: str,
            quantity: int = 1,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Sell ``quantity`` of ``item_id`` to the store covering the agent.

        The store only buys when ``buy_price > 0`` and has room under
        ``stock_cap``; the cap check lives in the conditional UPDATE so two
        concurrent sellers cannot overfill.
        """

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
            found = self._find_product(session, world_id, item_id, agent.location_id)
            if found is None:
                return False, None, MSG_PRODUCT_MISSING
            product, store = found
            if agent.location_id != store.location_id:
                return False, None, MSG_NOT_AT_STORE
            if not self._store_open(session, world_id, store, world.world_time):
                return False, None, MSG_STORE_CLOSED  # R8
            if product.buy_price <= 0:
                return False, None, MSG_NOT_BUYABLE
            # M19: a personal store may buy (owner pays from own balance);
            # unbound stores would mint money (agent paid, nobody pays).
            owner: Agent | None = None
            if store.owner_agent_id:
                if store.owner_agent_id == agent_id:
                    return False, None, MSG_SELL_TO_SELF
                owner = session.get(
                    Agent,
                    {"world_id": world_id, "agent_id": store.owner_agent_id},
                )
                if owner is None:
                    return False, None, MSG_STORE_UNBOUND
            elif not store.company_id:
                return False, None, MSG_STORE_UNBOUND
            inventory = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if inventory is None or inventory.quantity < quantity:
                return False, None, MSG_NOT_IN_INVENTORY

            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            total = product.buy_price * quantity
            # M13 R33: the store pays from its owning company's account; no
            # company money -> the trade is rejected (no credit, R7). The
            # check must run BEFORE the stock update: retry_on_lock commits
            # regardless of fn's return value, so a late rejection would
            # otherwise leave the stock incremented without payment.
            company = (
                session.get(
                    Company,
                    {"world_id": world_id, "company_id": store.company_id},
                )
                if store.company_id
                else None
            )
            if company is not None and company.money < total:
                return False, None, "企业资金不足"
            if owner is not None and owner.money < total:
                return False, None, MSG_OWNER_POOR  # R7: no credit either

            result = session.execute(
                update(StoreProduct)
                .where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.store_id == store.store_id,
                    StoreProduct.item_id == item_id,
                    StoreProduct.stock + quantity <= StoreProduct.stock_cap,
                )
                .values(stock=StoreProduct.stock + quantity)
            )
            if result.rowcount == 0:
                return False, None, MSG_STORE_FULL

            agent.money += total
            inventory.quantity -= quantity
            if inventory.quantity <= 0:
                session.delete(inventory)
            if company is not None:
                company.money -= total
                session.add(
                    CompanyTransaction(
                        world_id=world_id,
                        company_id=company.company_id,
                        type="material_purchase",
                        amount=-total,
                        balance_after=company.money,
                        related_agent_id=agent_id,
                        related_item_id=item_id,
                        quantity=quantity,
                        reference_type="store",
                        reference_id=store.store_id,
                        reason=f"商店收购 {item_name}×{quantity}",
                        world_time=world.world_time,
                        trace_id=trace_id or "",
                    )
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "company_money_changed",
                    {
                        "company_id": company.company_id,
                        "amount": -total,
                        "balance": company.money,
                        "reason": f"商店收购 {item_name}×{quantity}",
                    },
                    trace_id,
                )
            elif owner is not None:
                # M19: personal-store purchase — the owner pays from their
                # own balance (mirror of the R41 sale settlement).
                owner.money -= total
                session.add(
                    Transaction(
                        world_id=world_id,
                        agent_id=owner.agent_id,
                        type="expense",
                        amount=-total,
                        balance_after=owner.money,
                        item_id=item_id,
                        quantity=quantity,
                        reason=f"店铺收购 {item_name}×{quantity}",
                        world_time=world.world_time,
                        trace_id=trace_id or "",
                    )
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "store_purchase_completed",
                    {
                        "store_id": store.store_id,
                        "owner_agent_id": owner.agent_id,
                        "seller_agent_id": agent_id,
                        "item_id": item_id,
                        "item_name": item_name,
                        "quantity": quantity,
                        "unit_price": product.buy_price,
                        "total": total,
                    },
                    trace_id,
                )
                runtime.event_bus.publish(
                    session,
                    world.world_time,
                    "money_changed",
                    {
                        "agent_id": owner.agent_id,
                        "amount": -total,
                        "balance": owner.money,
                        "reason": f"店铺收购 {item_name}×{quantity}",
                    },
                    trace_id,
                )
            session.add(
                Transaction(
                    world_id=world_id,
                    agent_id=agent_id,
                    type="income",
                    amount=total,
                    balance_after=agent.money,
                    item_id=item_id,
                    quantity=quantity,
                    reason=f"出售 {item_name}×{quantity}",
                    world_time=world.world_time,
                    trace_id=trace_id or "",
                )
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "item_sold",
                {
                    "agent_id": agent_id,
                    "item_id": item_id,
                    "item_name": item_name,
                    "quantity": quantity,
                    "unit_price": product.buy_price,
                    "total": total,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "money_changed",
                {
                    "agent_id": agent_id,
                    "amount": total,
                    "balance": agent.money,
                    "reason": f"出售 {item_name}×{quantity}",
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "inventory_changed",
                {
                    "agent_id": agent_id,
                    "items": self._inventory_list(session, world_id, agent_id),
                },
                trace_id,
            )
            return True, envelope, None

        return self._uow.run(_inner)

    # ------------------------------------------------------------------ #
    # Use (food)
    # ------------------------------------------------------------------ #

    def use_item(
            self,
            world_id: str,
            agent_id: str,
            item_id: str,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Consume one usable item: food restores satiety, M12 mood items
        restore mood (both may apply for hybrid items)."""
        session = self._session_factory()
        try:
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
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            if item is None:
                return False, None, MSG_ITEM_MISSING
            if item.satiety_restore <= 0 and item.mood_restore <= 0:
                return False, None, MSG_NOT_FOOD
            inventory = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if inventory is None or inventory.quantity < 1:
                return False, None, MSG_NOT_IN_INVENTORY

            satiety_before = agent.satiety
            satiety_after = min(100, satiety_before + item.satiety_restore)
            mood_before = agent.mood
            mood_after = min(100, mood_before + item.mood_restore)
            agent.satiety = satiety_after
            agent.mood = mood_after
            inventory.quantity -= 1
            if inventory.quantity <= 0:
                session.delete(inventory)

            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "item_used",
                {
                    "agent_id": agent_id,
                    "item_id": item_id,
                    "item_name": item.name,
                    "satiety_before": satiety_before,
                    "satiety_after": satiety_after,
                    "mood_before": mood_before,
                    "mood_after": mood_after,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "needs_changed",
                {
                    "agent_id": agent_id,
                    "satiety": agent.satiety,
                    "energy": agent.energy,
                    "mood": agent.mood,
                    "loneliness": agent.loneliness,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "inventory_changed",
                {
                    "agent_id": agent_id,
                    "items": self._inventory_list(session, world_id, agent_id),
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _find_product(
            self, session: Session, world_id: str, item_id: str,
            location_id: str | None = None,
    ) -> tuple[StoreProduct, Store] | None:
        """The (product, store) pair selling/buying ``item_id`` in this world.

        When ``location_id`` is given, prefer the store covering that
        location (M18: a personal shop and the village store may sell the
        same item at different prices — the agent buys from the shop it is
        standing in). Falls back to the unfiltered lookup for callers that
        have no location context.
        """
        if location_id:
            product = session.scalars(
                select(StoreProduct)
                .join(
                    Store,
                    (Store.world_id == StoreProduct.world_id)
                    & (Store.store_id == StoreProduct.store_id),
                )
                .where(
                    StoreProduct.world_id == world_id,
                    StoreProduct.item_id == item_id,
                    Store.location_id == location_id,
                )
            ).first()
            if product is not None:
                store = session.get(
                    Store, {"world_id": world_id, "store_id": product.store_id}
                )
                if store is not None:
                    return product, store
        product = session.scalars(
            select(StoreProduct).where(
                StoreProduct.world_id == world_id, StoreProduct.item_id == item_id
            )
        ).first()
        if product is None:
            return None
        store = session.get(Store, {"world_id": world_id, "store_id": product.store_id})
        if store is None:
            return None
        return product, store

    @staticmethod
    def _item_work_bonus(item: Item, job_id: str) -> int:
        """M19: per-job tool bonus wins over the flat work_bonus.

        ``item.work_bonus_jobs`` is a JSON object {job_id: bonus}; when the
        current job matches a key, that value applies, else the legacy flat
        bonus. Malformed JSON degrades to the flat bonus.
        """
        if item.work_bonus_jobs:
            try:
                mapping = json.loads(item.work_bonus_jobs)
            except (TypeError, ValueError):
                mapping = {}
            if isinstance(mapping, dict) and job_id in mapping:
                try:
                    return int(mapping[job_id])
                except (TypeError, ValueError):
                    pass
        return item.work_bonus

    @staticmethod
    def _store_open(
            session: Session, world_id: str, store: Store, world_time: int
    ) -> bool:
        location = session.get(WorldLocation, {"world_id": world_id, "location_id": store.location_id})
        if location is None:
            return False
        return is_location_open(
            location.location_type, location.open_hour, location.close_hour, world_time
        )

    @staticmethod
    def _add_inventory(
            session: Session, world_id: str, agent_id: str, item_id: str, quantity: int
    ) -> None:
        row = session.get(
            Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
        )
        if row is None:
            session.add(
                Inventory(
                    world_id=world_id,
                    agent_id=agent_id,
                    item_id=item_id,
                    quantity=quantity,
                )
            )
        else:
            row.quantity += quantity

    @staticmethod
    def _inventory_list(
            session: Session, world_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        # The caller may have added inventory rows not yet flushed
        # (autoflush is off); flush so the snapshot list is complete.
        session.flush()
        rows = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == world_id, Inventory.agent_id == agent_id)
            .order_by(Inventory.item_id)
        ).all()
        return [{"item_id": row.item_id, "quantity": row.quantity} for row in rows]

    def log_rejection(self, world_id: str, agent_id: str, reason: str) -> None:  # pragma: no cover
        logger.debug("Economy rejected world={} agent={}: {}", world_id, agent_id, reason)
