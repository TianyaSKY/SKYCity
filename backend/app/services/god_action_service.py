"""GodActionService (M7): god-view interventions with a full audit trail.

Every intervention flows: GodActionService -> god_actions audit row ->
god_action_applied event (first, so the M6 memory/relationship hooks fire) ->
command-specific events -> WS push -> agent memory. ``apply`` returns the
contract shape {command_id, success, result, events} where ``events`` lists
every envelope this intervention produced, in sequence order.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.companies import Company, CompanyTransaction, EmploymentContract
from app.database.models.crops import Crop
from app.database.models.god_actions import GodAction
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.locations import WorldLocation
from app.database.models.stocks import Stock, StockHolding
from app.database.models.stores import Store, StoreProduct
from app.database.models.structures import TileStructure
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.config.gameplay import VALID_SPEEDS, VALID_WEATHERS
from app.world_engine.engine import WorldEngine

COMMAND_TYPES = {
    "pause",
    "resume",
    "set_speed",
    "change_weather",
    "grant_money",
    "deduct_money",
    "spawn_item",
    "teleport",
    "public_event",
    "change_store_stock",
    "change_stock_price",
    # M14 (R22): god can place or demolish structures.
    "remove_structure",
    "build_structure",
    # M15 (R23): god can rewrite crop growth or clear a farm cell.
    "set_crop_stage",
    "remove_crop",
    "inject_company_money",
    # M18 (R43): god can force-close a personal store.
    "close_store",
}

MSG_WORLD_MISSING = "世界不存在"
MSG_UNKNOWN_COMMAND = "未知的神谕指令类型"
MSG_AGENT_MISSING = "智能体不存在"
MSG_ITEM_MISSING = "物品不存在"
MSG_LOCATION_MISSING = "地点不存在"
MSG_PRODUCT_MISSING = "商店没有该商品"
MSG_AMOUNT_REQUIRED = "金额必须为正整数"
MSG_QUANTITY_REQUIRED = "数量必须大于等于 0"
MSG_WEATHER_REQUIRED = "天气必须是 clear/cloudy/rain/snow"
MSG_SPEED_REQUIRED = "倍速必须是 1/2/5/10"
MSG_TEXT_REQUIRED = "事件文本不能为空"
MSG_STOCK_PRICE_REQUIRED = "股价必须为正整数"
MSG_STOCK_MISSING = "股票不存在"
MSG_STRUCTURE_MISSING = "该位置没有建筑"
MSG_BLUEPRINT_MISSING = "蓝图不存在"
MSG_CELL_OCCUPIED = "该位置已有建筑"
MSG_CELL_REQUIRED = "col/row 必须为地图内的整数坐标"
MSG_CROP_MISSING = "该位置没有作物"
MSG_STAGE_REQUIRED = "stage 必须为非负整数"
MSG_STORE_NOT_FOUND = "店铺不存在"

DEFAULT_STORE_ID = "village_shop"


class GodActionService:
    """Owns the god command gate for all worlds (one instance, like the
    ActionExecutionService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def apply(
            self,
            world_id: str,
            command_type: str,
            target_id: str | None = None,
            parameters: dict[str, Any] | None = None,
            reason: str = "",
    ) -> dict[str, Any]:
        """Apply one god command; returns {command_id, success, result, events}.

        Raises HTTPException(400) for unknown command types or invalid
        parameters, HTTPException(404) for missing world/target rows.
        """
        if command_type not in COMMAND_TYPES:
            raise HTTPException(status_code=400, detail=MSG_UNKNOWN_COMMAND)
        runtime = self.engine.get_runtime(world_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
        parameters = parameters or {}
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None:
                raise HTTPException(status_code=404, detail=MSG_WORLD_MISSING)
            world_time = runtime.clock.world_time
            command_id = f"cmd_{uuid.uuid4().hex[:12]}"
            trace_id = f"trc_{uuid.uuid4().hex[:16]}"

            handler = getattr(self, f"_cmd_{command_type}")
            result, events = handler(
                session, runtime, world, command_id, trace_id, world_time,
                target_id, parameters, reason,
            )
            session.add(
                GodAction(
                    command_id=command_id,
                    world_id=world_id,
                    command_type=command_type,
                    target_id=target_id,
                    parameters_json=parameters,
                    reason=reason,
                    created_at=world_time,
                    result_json=result,
                    success=True,
                )
            )
            session.commit()
            logger.info(
                "God action {} {} -> {} (target={}, result={})",
                command_id, command_type, world_id, target_id, result,
            )
            return {
                "command_id": command_id,
                "success": True,
                "result": result,
                "events": [envelope.model_dump() for envelope in events],
            }
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _announce(
            self,
            session: Session,
            runtime: Any,
            command_type: str,
            command_id: str,
            trace_id: str,
            world_time: int,
            target_id: str | None,
            parameters: dict[str, Any],
            reason: str,
            result: dict[str, Any],
    ) -> Any:
        """Publish god_action_applied FIRST (memory/relationship hooks fire)."""
        return runtime.event_bus.publish(
            session,
            world_time,
            "god_action_applied",
            {
                "command_id": command_id,
                "command_type": command_type,
                "target_id": target_id,
                "parameters": parameters,
                "reason": reason,
                "result": result,
            },
            trace_id,
        )

    def _agent(
            self, session: Session, world_id: str, target_id: str | None
    ) -> Agent:
        if not target_id:
            raise HTTPException(status_code=404, detail=MSG_AGENT_MISSING)
        agent = session.get(Agent, {"world_id": world_id, "agent_id": target_id})
        if agent is None:
            raise HTTPException(status_code=404, detail=MSG_AGENT_MISSING)
        return agent

    @staticmethod
    def _inventory_list(
            session: Session, world_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        session.flush()  # include rows added in this transaction
        rows = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == world_id, Inventory.agent_id == agent_id)
            .order_by(Inventory.item_id)
        ).all()
        return [{"item_id": row.item_id, "quantity": row.quantity} for row in rows]

    # ------------------------------------------------------------------ #
    # Clock commands (pause / resume / set_speed)
    # ------------------------------------------------------------------ #

    def _cmd_pause(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        announce = self._announce(
            session, runtime, "pause", command_id, trace_id, world_time,
            target_id, parameters, reason, {"paused": True},
        )
        # Delegate to the engine method (it owns the clock + decision re-arm);
        # its world_paused event lands after god_action_applied in the stream.
        found, envelope = self.engine.set_paused(world.world_id, True)
        if envelope is not None:
            return {"paused": True}, [announce, envelope]
        return {"paused": True, "already": True}, [announce]

    def _cmd_resume(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        announce = self._announce(
            session, runtime, "resume", command_id, trace_id, world_time,
            target_id, parameters, reason, {"resumed": True},
        )
        found, envelope = self.engine.set_paused(world.world_id, False)
        if envelope is not None:
            return {"resumed": True}, [announce, envelope]
        return {"resumed": True, "already": True}, [announce]

    def _cmd_set_speed(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        speed = parameters.get("speed")
        if speed not in VALID_SPEEDS:
            raise HTTPException(status_code=400, detail=MSG_SPEED_REQUIRED)
        announce = self._announce(
            session, runtime, "set_speed", command_id, trace_id, world_time,
            target_id, parameters, reason, {"speed": speed},
        )
        found, envelope = self.engine.set_speed(world.world_id, speed)
        if envelope is not None:
            return {"speed": speed}, [announce, envelope]
        return {"speed": speed, "already": True}, [announce]

    # ------------------------------------------------------------------ #
    # Weather
    # ------------------------------------------------------------------ #

    def _cmd_change_weather(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        weather = parameters.get("weather")
        if weather not in VALID_WEATHERS:
            raise HTTPException(status_code=400, detail=MSG_WEATHER_REQUIRED)
        world.weather = weather
        result = {"weather": weather}
        announce = self._announce(
            session, runtime, "change_weather", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        changed = runtime.event_bus.publish(
            session, world_time, "weather_changed", {"weather": weather}, trace_id
        )
        return result, [announce, changed]

    # ------------------------------------------------------------------ #
    # Money (grant / deduct)
    # ------------------------------------------------------------------ #

    def _cmd_grant_money(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        amount = int(parameters.get("amount") or 0)
        if amount <= 0:
            raise HTTPException(status_code=400, detail=MSG_AMOUNT_REQUIRED)
        agent = self._agent(session, world.world_id, target_id)
        agent.money += amount
        note = reason or "神谕赐予"
        session.add(
            Transaction(
                world_id=world.world_id,
                agent_id=agent.agent_id,
                type="god_grant",
                amount=amount,
                balance_after=agent.money,
                item_id=None,
                quantity=None,
                reason=f"神谕赐予：{note}",
                world_time=world_time,
                trace_id=trace_id,
            )
        )
        result = {"agent_id": agent.agent_id, "amount": amount, "balance": agent.money}
        announce = self._announce(
            session, runtime, "grant_money", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        money = runtime.event_bus.publish(
            session, world_time, "money_changed",
            {
                "agent_id": agent.agent_id,
                "amount": amount,
                "balance": agent.money,
                "reason": f"神谕赐予：{note}",
            },
            trace_id,
        )
        return result, [announce, money]

    def _cmd_inject_company_money(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """M13: 上帝注资企业账户；资金足够时立即补发欠薪（R29/R32）."""
        amount = int(parameters.get("amount") or 0)
        if amount <= 0:
            raise HTTPException(status_code=400, detail=MSG_AMOUNT_REQUIRED)
        company = session.get(
            Company, {"world_id": world.world_id, "company_id": target_id}
        )
        if company is None:
            raise HTTPException(status_code=404, detail="企业不存在")
        company.money += amount
        note = reason or "神谕注资"
        session.add(
            CompanyTransaction(
                world_id=world.world_id,
                company_id=company.company_id,
                type="god_injection",
                amount=amount,
                balance_after=company.money,
                reference_type="company",
                reference_id=company.company_id,
                reason=f"神谕注资：{note}",
                world_time=world_time,
                trace_id=trace_id,
            )
        )
        changed = runtime.event_bus.publish(
            session, world_time, "company_money_changed",
            {
                "company_id": company.company_id,
                "amount": amount,
                "balance": company.money,
                "reason": f"神谕注资：{note}",
            },
            trace_id,
        )
        # Repay outstanding unpaid wages the company can now afford.
        repaid_total = 0
        company_service = getattr(self.engine, "company_employment_service", None)
        if company_service is not None:
            contracts = session.scalars(
                select(EmploymentContract).where(
                    EmploymentContract.world_id == world.world_id,
                    EmploymentContract.company_id == company.company_id,
                    EmploymentContract.status.in_(("active", "on_leave")),
                    EmploymentContract.unpaid_wage > 0,
                )
            ).all()
            for contract in contracts:
                agent = session.get(
                    Agent, {"world_id": world.world_id, "agent_id": contract.agent_id}
                )
                if agent is None:
                    continue
                repaid_total += company_service.payroll.repay_contract(
                    session, world, contract, company, agent, trace_id
                )
        result = {
            "company_id": company.company_id,
            "amount": amount,
            "balance": company.money,
            "repaid_total": repaid_total,
        }
        announce = self._announce(
            session, runtime, "inject_company_money", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        return result, [announce, changed]

    def _cmd_deduct_money(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        amount = int(parameters.get("amount") or 0)
        if amount <= 0:
            raise HTTPException(status_code=400, detail=MSG_AMOUNT_REQUIRED)
        agent = self._agent(session, world.world_id, target_id)
        actual = min(amount, agent.money)  # clamp at 0 balance (no credit, R7)
        agent.money -= actual
        note = reason or "神谕扣除"
        session.add(
            Transaction(
                world_id=world.world_id,
                agent_id=agent.agent_id,
                type="god_deduct",
                amount=-actual,
                balance_after=agent.money,
                item_id=None,
                quantity=None,
                reason=f"神谕扣除：{note}",
                world_time=world_time,
                trace_id=trace_id,
            )
        )
        result = {
            "agent_id": agent.agent_id,
            "requested": amount,
            "actual": actual,
            "balance": agent.money,
        }
        announce = self._announce(
            session, runtime, "deduct_money", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        money = runtime.event_bus.publish(
            session, world_time, "money_changed",
            {
                "agent_id": agent.agent_id,
                "amount": -actual,
                "balance": agent.money,
                "reason": f"神谕扣除：{note}",
            },
            trace_id,
        )
        return result, [announce, money]

    # ------------------------------------------------------------------ #
    # Items (spawn)
    # ------------------------------------------------------------------ #

    def _cmd_spawn_item(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        item_id = str(parameters.get("item_id") or "")
        quantity = int(parameters.get("quantity") or 1)
        if not item_id:
            raise HTTPException(status_code=400, detail=MSG_ITEM_MISSING)
        if quantity < 1:
            raise HTTPException(status_code=400, detail=MSG_QUANTITY_REQUIRED)
        agent = self._agent(session, world.world_id, target_id)
        item = session.get(Item, {"world_id": world.world_id, "item_id": item_id})
        if item is None:
            raise HTTPException(status_code=404, detail=MSG_ITEM_MISSING)
        row = session.get(
            Inventory,
            {"world_id": world.world_id, "agent_id": agent.agent_id, "item_id": item_id},
        )
        if row is None:
            session.add(
                Inventory(
                    world_id=world.world_id,
                    agent_id=agent.agent_id,
                    item_id=item_id,
                    quantity=quantity,
                )
            )
        else:
            row.quantity += quantity
        result = {
            "agent_id": agent.agent_id,
            "item_id": item_id,
            "item_name": item.name,
            "quantity": quantity,
        }
        announce = self._announce(
            session, runtime, "spawn_item", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        spawned = runtime.event_bus.publish(
            session, world_time, "item_spawned",
            {
                "agent_id": agent.agent_id,
                "item_id": item_id,
                "item_name": item.name,
                "quantity": quantity,
            },
            trace_id,
        )
        inventory = runtime.event_bus.publish(
            session, world_time, "inventory_changed",
            {
                "agent_id": agent.agent_id,
                "items": self._inventory_list(session, world.world_id, agent.agent_id),
            },
            trace_id,
        )
        return result, [announce, spawned, inventory]

    # ------------------------------------------------------------------ #
    # Teleport
    # ------------------------------------------------------------------ #

    def _cmd_teleport(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        location_id = str(parameters.get("location_id") or "")
        if not location_id:
            raise HTTPException(status_code=400, detail=MSG_LOCATION_MISSING)
        agent = self._agent(session, world.world_id, target_id)
        location = session.get(
            WorldLocation, {"world_id": world.world_id, "location_id": location_id}
        )
        if location is None:
            raise HTTPException(status_code=404, detail=MSG_LOCATION_MISSING)
        agent.col = location.col
        agent.row = location.row
        agent.location_id = location_id
        # Cancel the current action (move/wait/work) — stale completions are
        # guarded by action_type in the scheduler handlers.
        agent.action_type = None
        agent.action_started_at = None
        agent.action_ends_at = None
        agent.action_data = None
        result = {
            "agent_id": agent.agent_id,
            "to": [location.col, location.row],
            "location_id": location_id,
        }
        announce = self._announce(
            session, runtime, "teleport", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        teleport = runtime.event_bus.publish(
            session, world_time, "god_teleport",
            {
                "agent_id": agent.agent_id,
                "to": [location.col, location.row],
                "location_id": location_id,
                "reason": reason,
            },
            trace_id,
        )
        state = runtime.event_bus.publish(
            session, world_time, "agent_state_changed",
            {
                "agent_id": agent.agent_id,
                "state": {
                    "col": agent.col,
                    "row": agent.row,
                    "location_id": agent.location_id,
                    "action": None,
                },
            },
            trace_id,
        )
        # Commit before the conversation check so its session sees the new
        # position (same pattern as the move_completed handler).
        session.commit()
        if self.engine.conversation_service is not None:
            self.engine.conversation_service.end_if_distance_exceeded(
                world.world_id, agent.agent_id
            )
        return result, [announce, teleport, state]

    # ------------------------------------------------------------------ #
    # Public events
    # ------------------------------------------------------------------ #

    def _cmd_public_event(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        text = str(parameters.get("text") or "")
        if not text:
            raise HTTPException(status_code=400, detail=MSG_TEXT_REQUIRED)
        result = {"text": text, "importance": 0.8}
        announce = self._announce(
            session, runtime, "public_event", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        # No agent_id -> public: the MemoryRecorder records it for every agent.
        created = runtime.event_bus.publish(
            session, world_time, "world_event_created",
            {"text": text, "importance": 0.8},
            trace_id,
        )
        return result, [announce, created]

    # ------------------------------------------------------------------ #
    # Store stock
    # ------------------------------------------------------------------ #

    def _cmd_change_store_stock(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        item_id = str(parameters.get("item_id") or "")
        quantity = int(parameters.get("quantity") or 0)
        if not item_id:
            raise HTTPException(status_code=400, detail=MSG_PRODUCT_MISSING)
        if quantity < 0:
            raise HTTPException(status_code=400, detail=MSG_QUANTITY_REQUIRED)
        store_id = target_id or DEFAULT_STORE_ID
        product = session.get(
            StoreProduct,
            {"world_id": world.world_id, "store_id": store_id, "item_id": item_id},
        )
        if product is None:
            raise HTTPException(status_code=404, detail=MSG_PRODUCT_MISSING)
        product.stock = quantity
        result = {"store_id": store_id, "item_id": item_id, "quantity": quantity}
        announce = self._announce(
            session, runtime, "change_store_stock", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        stock = runtime.event_bus.publish(
            session, world_time, "store_stock_changed",
            {"store_id": store_id, "item_id": item_id, "quantity": quantity},
            trace_id,
        )
        return result, [announce, stock]

    def _cmd_close_store(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """Force-close a personal store (R43): shelf stock returns to the
        owner's backpack, the store (and a wild-cell stall location) and its
        stock listing are deleted, and ``store_closed`` is published with
        reason=上帝干预."""
        store = session.get(
            Store, {"world_id": world.world_id, "store_id": target_id}
        )
        if store is None:
            raise HTTPException(status_code=404, detail=MSG_STORE_NOT_FOUND)
        products = session.scalars(
            select(StoreProduct).where(
                StoreProduct.world_id == world.world_id,
                StoreProduct.store_id == store.store_id,
            )
        ).all()
        for product in products:
            if product.stock > 0 and store.owner_agent_id:
                self._refund_inventory(
                    session,
                    world.world_id,
                    store.owner_agent_id,
                    product.item_id,
                    product.stock,
                )
            session.delete(product)
        # R43: 歇业即退市 — the listing and any holdings disappear (explicit
        # deletes: SQLite sessions run with FKs off, so ondelete=CASCADE on
        # stock_holdings/stores would not fire).
        for stock in session.scalars(
                select(Stock).where(
                    Stock.world_id == world.world_id,
                    Stock.company_id == store.store_id,
                    Stock.source == "store",
                )
        ).all():
            for holding in session.scalars(
                    select(StockHolding).where(
                        StockHolding.world_id == world.world_id,
                        StockHolding.stock_id == stock.stock_id,
                    )
            ).all():
                session.delete(holding)
            session.delete(stock)
        map_location_ids = {
            loc.location_id for loc in self.engine.world_config.locations
        }
        if store.location_id not in map_location_ids:
            location_row = session.get(
                WorldLocation,
                {"world_id": world.world_id, "location_id": store.location_id},
            )
            if location_row is not None:
                session.delete(location_row)
        result = {
            "store_id": store.store_id,
            "owner_agent_id": store.owner_agent_id,
            "name": store.name,
        }
        announce = self._announce(
            session, runtime, "close_store", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        closed = runtime.event_bus.publish(
            session, world_time, "store_closed",
            {
                "store_id": store.store_id,
                "owner_agent_id": store.owner_agent_id,
                "reason": "上帝干预",
            },
            trace_id,
        )
        session.delete(store)
        return result, [announce, closed]

    # ------------------------------------------------------------------ #
    # Stock price (M10, R18.4)
    # ------------------------------------------------------------------ #

    def _cmd_change_stock_price(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        price = parameters.get("price")
        if not isinstance(price, int) or isinstance(price, bool) or price < 1:
            raise HTTPException(status_code=400, detail=MSG_STOCK_PRICE_REQUIRED)
        stock_id = str(parameters.get("stock_id") or "")
        stock = session.get(
            Stock, {"world_id": world.world_id, "stock_id": stock_id}
        )
        if stock is None:
            raise HTTPException(status_code=404, detail=MSG_STOCK_MISSING)
        stock.price = price
        result = {"stock_id": stock.stock_id, "price": price}
        announce = self._announce(
            session, runtime, "change_stock_price", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        changed = runtime.event_bus.publish(
            session, world_time, "stock_price_changed",
            {
                "stock_id": stock.stock_id,
                "stock_name": stock.name,
                "price": stock.price,
                "prev_price": stock.prev_price,
                "day_business": stock.day_business,
            },
            trace_id,
        )
        return result, [announce, changed]

    # ------------------------------------------------------------------ #
    # Structures (M14, R22)
    # ------------------------------------------------------------------ #

    def _cmd_remove_structure(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """Demolish a structure. Interrupting a build (status="building")
        refunds materials proportionally to remaining time (R22.2) and frees
        the builder's action; a completed structure is simply removed."""
        col, row = self._structure_cell(parameters)
        structure = session.get(
            TileStructure,
            {"world_id": world.world_id, "col": col, "row": row},
        )
        if structure is None:
            raise HTTPException(status_code=404, detail=MSG_STRUCTURE_MISSING)
        events: list[Any] = []
        refunded: list[dict[str, Any]] = []
        if structure.status == "building":
            # Cancel the owner's in-flight build + refund proportionally.
            owner = session.get(
                Agent,
                {"world_id": world.world_id, "agent_id": structure.owner_agent_id},
            )
            blueprint = self.engine.blueprints.get(structure.blueprint_id)
            if owner is not None and blueprint is not None:
                started = owner.action_started_at or world_time
                elapsed = max(world_time - started, 0)
                remaining = max(blueprint.duration_minutes - elapsed, 0)
                for item_id, total in (structure.materials_json or {}).items():
                    # R22.2: refund proportional to remaining time, min 1.
                    back = max(1, total * remaining // blueprint.duration_minutes)
                    self._refund_inventory(
                        session, world.world_id, owner.agent_id, item_id, back
                    )
                    refunded.append({"item_id": item_id, "quantity": back})
                if owner.action_type == "build":
                    owner.action_type = None
                    owner.action_started_at = None
                    owner.action_ends_at = None
                    owner.action_data = None
                    runtime.scheduler.cancel_for_agent(session, owner.agent_id)
                events.append(
                    runtime.event_bus.publish(
                        session, world_time, "inventory_changed",
                        {
                            "agent_id": owner.agent_id,
                            "items": self._inventory_list(session, world.world_id, owner.agent_id),
                        },
                        trace_id,
                    )
                )
        result = {
            "col": col,
            "row": row,
            "blueprint_id": structure.blueprint_id,
            "status": structure.status,
            "refunded": refunded,
        }
        announce = self._announce(
            session, runtime, "remove_structure", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        removed = runtime.event_bus.publish(
            session, world_time, "structure_removed",
            {
                "col": col,
                "row": row,
                "blueprint_id": structure.blueprint_id,
                "removed_by": target_id,
            },
            trace_id,
        )
        session.delete(structure)
        return result, [announce, removed, *events]

    def _cmd_build_structure(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """God places a completed structure directly (R13: god may bypass the
        build process, but never invents blueprints or occupies an
        existing cell)."""
        col, row = self._structure_cell(parameters)
        blueprint_id = str(parameters.get("blueprint_id") or "")
        blueprint = self.engine.blueprints.get(blueprint_id)
        if blueprint is None:
            raise HTTPException(status_code=400, detail=MSG_BLUEPRINT_MISSING)
        existing = session.get(
            TileStructure,
            {"world_id": world.world_id, "col": col, "row": row},
        )
        if existing is not None:
            raise HTTPException(status_code=400, detail=MSG_CELL_OCCUPIED)
        session.add(
            TileStructure(
                world_id=world.world_id,
                col=col,
                row=row,
                blueprint_id=blueprint_id,
                owner_agent_id=target_id or "",
                status="built",
                built_at=world_time,
                materials_json={},
            )
        )
        result = {
            "col": col,
            "row": row,
            "blueprint_id": blueprint_id,
            "owner_agent_id": target_id,
        }
        announce = self._announce(
            session, runtime, "build_structure", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        built = runtime.event_bus.publish(
            session, world_time, "structure_built",
            {
                "agent_id": target_id,
                "col": col,
                "row": row,
                "blueprint_id": blueprint_id,
                "owner_agent_id": target_id,
            },
            trace_id,
        )
        return result, [announce, built]

    def _structure_cell(self, parameters: dict[str, Any]) -> tuple[int, int]:
        """Validate {col, row} params -> (col, row) or 400."""
        col = parameters.get("col")
        row = parameters.get("row")
        if (
                not isinstance(col, int)
                or isinstance(col, bool)
                or not isinstance(row, int)
                or isinstance(row, bool)
        ):
            raise HTTPException(status_code=400, detail=MSG_CELL_REQUIRED)
        return col, row

    # ------------------------------------------------------------------ #
    # Crops (M15, R23)
    # ------------------------------------------------------------------ #

    def _cmd_set_crop_stage(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """Jump a crop to a given stage: recompute next_stage_at and schedule
        a fresh crop_grow callback. Stale callbacks from before the rewrite
        are skipped by the next_stage_at guard (R23.5)."""
        col, row = self._structure_cell(parameters)
        stage = parameters.get("stage")
        if not isinstance(stage, int) or isinstance(stage, bool) or stage < 0:
            raise HTTPException(status_code=400, detail=MSG_STAGE_REQUIRED)
        crop = session.get(
            Crop, {"world_id": world.world_id, "col": col, "row": row}
        )
        if crop is None:
            raise HTTPException(status_code=404, detail=MSG_CROP_MISSING)
        crop_def = self.engine.crops.get(crop.item_id)
        if crop_def is None:
            raise HTTPException(status_code=404, detail=MSG_CROP_MISSING)
        if stage >= len(crop_def.stages):
            raise HTTPException(status_code=400, detail=MSG_STAGE_REQUIRED)
        crop.stage = stage
        crop.next_stage_at = None
        if stage < len(crop_def.stages) - 1:
            crop.next_stage_at = world_time + crop_def.stages[stage][0]
            runtime.scheduler.schedule(
                session,
                crop.planted_by,
                "crop_grow",
                crop.next_stage_at,
                {"col": col, "row": row, "stage": stage, "trace_id": trace_id},
            )
        result = {"col": col, "row": row, "item_id": crop.item_id, "stage": stage}
        announce = self._announce(
            session, runtime, "set_crop_stage", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        grown = runtime.event_bus.publish(
            session, world_time, "crop_grown",
            {"col": col, "row": row, "item_id": crop.item_id, "stage": stage},
            trace_id,
        )
        return result, [announce, grown]

    def _cmd_remove_crop(
            self, session, runtime, world, command_id, trace_id, world_time,
            target_id, parameters, reason,
    ):
        """Clear a farm cell (crops hold no materials, so no refund)."""
        col, row = self._structure_cell(parameters)
        crop = session.get(
            Crop, {"world_id": world.world_id, "col": col, "row": row}
        )
        if crop is None:
            raise HTTPException(status_code=404, detail=MSG_CROP_MISSING)
        result = {"col": col, "row": row, "item_id": crop.item_id, "stage": crop.stage}
        announce = self._announce(
            session, runtime, "remove_crop", command_id, trace_id, world_time,
            target_id, parameters, reason, result,
        )
        removed = runtime.event_bus.publish(
            session, world_time, "crop_harvested",
            {
                "agent_id": crop.planted_by,
                "col": col,
                "row": row,
                "item_id": crop.item_id,
                "item_name": crop.item_id,
                "products": [],
                "removed_by": target_id,
            },
            trace_id,
        )
        session.delete(crop)
        return result, [announce, removed]

    @staticmethod
    def _refund_inventory(
            session: Session, world_id: str, agent_id: str, item_id: str, quantity: int
    ) -> None:
        row = session.get(
            Inventory,
            {"world_id": world_id, "agent_id": agent_id, "item_id": item_id},
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
