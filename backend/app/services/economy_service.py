"""EconomyService: the economy rule gate — work / buy / sell / use (M5).

World rules enforced here (docs/world-rules.md): R1 (one action; move 独占),
R3 (work uninterruptible — no other tool may run mid-work), R4 (last-item
race via BEGIN IMMEDIATE + conditional UPDATE), R7 (no credit), R8 (store
hours), R10 (wage + products settled at completion), R11 (hunger=100 blocks
work), R12 (energy=0 blocks work; forced rest lives in the decision service),
R14 (work drains energy by the job's intensity).

Every accepted action publishes its event envelopes through the runtime event
bus and returns ``(ok, envelope, reason)`` — the same shape the action
service uses, so tools and the HTTP layer stay uniform. Buy/sell run inside a
retrying BEGIN IMMEDIATE transaction (app.database.unit_of_work).
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.jobs import Employment, Job
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import Store, StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.unit_of_work import UnitOfWork
from app.world_engine.engine import WorldEngine, is_location_open

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_WORLD_MISSING = "世界不存在"
MSG_PAUSED = "世界已暂停"
MSG_AGENT_MISSING = "智能体不存在"
MSG_BUSY = "当前行动未完成"
MSG_JOB_MISSING = "工作不存在"
MSG_NOT_AT_JOB = "不在工作地点"
MSG_LOCATION_CLOSED = "地点未开门"
MSG_HUNGRY_FULL = "饥饿值已满，无法工作"
MSG_EXHAUSTED = "精力耗尽，无法工作"
MSG_PRODUCT_MISSING = "商店没有该商品"
MSG_NOT_AT_STORE = "不在商店"
MSG_STORE_CLOSED = "商店未开门"
MSG_NO_MONEY = "余额不足"
MSG_NO_STOCK = "库存不足"
MSG_NOT_IN_INVENTORY = "背包中没有该物品"
MSG_NOT_BUYABLE = "商店不收购该物品"
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
            if agent.location_id != job.location_id:
                return False, None, MSG_NOT_AT_JOB
            location = session.get(
                WorldLocation, {"world_id": world_id, "location_id": job.location_id}
            )
            if location is not None and not is_location_open(
                location.location_type, location.open_hour, location.close_hour, world.world_time
            ):
                return False, None, MSG_LOCATION_CLOSED  # R8
            if agent.hunger >= 100:
                return False, None, MSG_HUNGRY_FULL  # R11
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

        energy_spent = max(int(job.energy_cost_per_hour * job.duration_minutes / 60), 0)
        agent.energy = max(0, agent.energy - energy_spent)  # R14
        agent.money += job.wage  # R10

        produced: list[dict[str, Any]] = []
        for product in job.products_json or []:
            item_id = str(product.get("item_id") or "")
            quantity = int(product.get("quantity") or 0)
            if not item_id or quantity <= 0:
                continue
            if session.get(Item, {"world_id": action.world_id, "item_id": item_id}) is None:
                continue  # products only materialise for known items
            self._add_inventory(session, action.world_id, action.agent_id, item_id, quantity)
            produced.append({"item_id": item_id, "quantity": quantity})

        employment = session.get(
            Employment,
            {"world_id": action.world_id, "agent_id": action.agent_id, "job_id": job_id},
        )
        if employment is None:
            employment = Employment(
                world_id=action.world_id,
                agent_id=action.agent_id,
                job_id=job_id,
                hours_worked=0.0,
                total_earned=0,
            )
            session.add(employment)
        employment.hours_worked += job.duration_minutes / 60.0
        employment.total_earned += job.wage

        session.add(
            Transaction(
                world_id=action.world_id,
                agent_id=action.agent_id,
                type="work_wage",
                amount=job.wage,
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
                "wage": job.wage,
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
                "amount": job.wage,
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
            found = self._find_product(session, world_id, item_id)
            if found is None:
                return False, None, MSG_PRODUCT_MISSING
            product, store = found
            if agent.location_id != store.location_id:
                return False, None, MSG_NOT_AT_STORE
            if not self._store_open(session, world_id, store, world.world_time):
                return False, None, MSG_STORE_CLOSED  # R8

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
            found = self._find_product(session, world_id, item_id)
            if found is None:
                return False, None, MSG_PRODUCT_MISSING
            product, store = found
            if agent.location_id != store.location_id:
                return False, None, MSG_NOT_AT_STORE
            if not self._store_open(session, world_id, store, world.world_time):
                return False, None, MSG_STORE_CLOSED  # R8
            if product.buy_price <= 0:
                return False, None, MSG_NOT_BUYABLE
            inventory = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if inventory is None or inventory.quantity < quantity:
                return False, None, MSG_NOT_IN_INVENTORY

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

            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            item_name = item.name if item is not None else item_id
            total = product.buy_price * quantity
            agent.money += total
            inventory.quantity -= quantity
            if inventory.quantity <= 0:
                session.delete(inventory)
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
        """Consume one food item to restore hunger (R14)."""
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
            if item.hunger_restore <= 0:
                return False, None, MSG_NOT_FOOD
            inventory = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if inventory is None or inventory.quantity < 1:
                return False, None, MSG_NOT_IN_INVENTORY

            hunger_before = agent.hunger
            hunger_after = max(0, hunger_before - item.hunger_restore)
            agent.hunger = hunger_after
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
                    "hunger_before": hunger_before,
                    "hunger_after": hunger_after,
                },
                trace_id,
            )
            runtime.event_bus.publish(
                session,
                world.world_time,
                "needs_changed",
                {"agent_id": agent_id, "hunger": agent.hunger, "energy": agent.energy},
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
        self, session: Session, world_id: str, item_id: str
    ) -> tuple[StoreProduct, Store] | None:
        """The (product, store) pair selling/buying ``item_id`` in this world."""
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
