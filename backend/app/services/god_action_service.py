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
from app.database.models.god_actions import GodAction
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.locations import WorldLocation
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
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
}
VALID_WEATHERS = {"clear", "cloudy", "rain", "snow"}
VALID_SPEEDS = {1, 2, 5, 10}

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
