"""CropService (M15): the farming rule gate — plant / grow / harvest.

World rules enforced here (docs/world-rules.md):
- R23.1: plant/harvest are exclusive actions (idle only, like build).
- R23.2: planting is limited to the farm_field zone (R23.2 config).
- R23.3: the target cell must hold no crop and no structure (cross-table
  occupancy; the composite PK (world_id, col, row) is the race guard).
- R23.4: planting consumes one seed from the inventory.
- R23.5: growth advances on the world scheduler (crop_grow callbacks);
  the handler is idempotent — a stale callback (harvested/removed/rewritten
  crop) is a no-op.
- R23.6: only the final stage is harvestable; yield = config + held
  fertilizer yield_bonus (same sum as M12 C4); harvest clears the cell.
- R23.7: crops never block movement (no effective_walkable impact).
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import MAX_PLANT_DISTANCE
from app.database.models.agents import Agent
from app.database.models.crops import Crop
from app.database.models.inventories import Inventory
from app.database.models.items import Item
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.structures import TileStructure
from app.database.models.worlds import World
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_WORLD_MISSING = "世界不存在"
MSG_PAUSED = "世界已暂停"
MSG_AGENT_MISSING = "智能体不存在"
MSG_BUSY = "当前行动未完成"
MSG_CROP_UNKNOWN = "该种子无法种植"
MSG_OUT_OF_BOUNDS = "目标超出地图范围"
MSG_NOT_PLANTABLE = "这里不是农田，不能种植"
MSG_CELL_OCCUPIED = "该位置已被占用"
MSG_TOO_FAR = "离目标格太远"
MSG_NO_SEED = "背包中没有种子"
MSG_CROP_MISSING = "这里没有作物"
MSG_NOT_RIPE = "作物还没成熟"


class CropService:
    """Owns the farming rule gate for all worlds (one instance, like the
    ActionExecutionService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Plant (R23.1 ~ R23.4)
    # ------------------------------------------------------------------ #

    def plant(
            self,
            world_id: str,
            agent_id: str,
            col: int | None,
            row: int | None,
            item_id: str | None,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Validate + plant a seed (R23.1~23.4)."""
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
                return False, None, MSG_BUSY  # R23.1: exclusive
            if col is None or row is None:
                return False, None, MSG_OUT_OF_BOUNDS
            crop_def = self.engine.crops.get(item_id or "")
            if crop_def is None:
                return False, None, MSG_CROP_UNKNOWN
            if (col, row) not in self.engine.plantable_cells:
                return False, None, MSG_NOT_PLANTABLE  # R23.2
            if session.get(
                    Crop, {"world_id": world_id, "col": col, "row": row}
            ) is not None:
                return False, None, MSG_CELL_OCCUPIED  # R23.3
            if session.get(
                    TileStructure, {"world_id": world_id, "col": col, "row": row}
            ) is not None:
                return False, None, MSG_CELL_OCCUPIED  # R23.3 cross-table
            if abs(agent.col - col) + abs(agent.row - row) > MAX_PLANT_DISTANCE:
                return False, None, MSG_TOO_FAR  # R23.1
            seed = session.get(
                Inventory,
                {"world_id": world_id, "agent_id": agent_id, "item_id": item_id},
            )
            if seed is None or seed.quantity < 1:
                return False, None, MSG_NO_SEED  # R23.4

            seed.quantity -= 1
            if seed.quantity <= 0:
                session.delete(seed)
            first_minutes = crop_def.stages[0][0]
            next_stage_at = world.world_time + first_minutes
            session.add(
                Crop(
                    world_id=world_id,
                    col=col,
                    row=row,
                    item_id=item_id,
                    planted_by=agent_id,
                    planted_at=world.world_time,
                    stage=0,
                    next_stage_at=next_stage_at,
                )
            )
            # R23.5: schedule the first growth callback.
            runtime.scheduler.schedule(
                session,
                agent_id,
                "crop_grow",
                next_stage_at,
                {"col": col, "row": row, "stage": 0, "trace_id": trace_id},
            )
            item = session.get(Item, {"world_id": world_id, "item_id": item_id})
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "crop_planted",
                {
                    "agent_id": agent_id,
                    "col": col,
                    "row": row,
                    "item_id": item_id,
                    "item_name": item.name if item is not None else item_id,
                    "stage": 0,
                    "next_stage_at": next_stage_at,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Scheduler handler (growth, R23.5)
    # ------------------------------------------------------------------ #

    def handle_crop_grow(self, session: Session, action: ScheduledAction) -> None:
        """Scheduler handler for "crop_grow": advance one growth stage.

        Idempotent: fires only when the crop still exists AND its stage and
        next_stage_at match the callback — a harvested/removed/rewritten
        crop makes the callback a no-op (R23.5).
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        payload = action.payload or {}
        col = int(payload.get("col") or -1)
        row = int(payload.get("row") or -1)
        crop = session.get(Crop, {"world_id": action.world_id, "col": col, "row": row})
        if crop is None or crop.next_stage_at != action.due_at:
            return  # stale: harvested, removed, or rewritten by god
        crop_def = self.engine.crops.get(crop.item_id)
        if crop_def is None or crop.stage >= len(crop_def.stages) - 1:
            return  # unknown crop or already final — nothing to advance
        world_time = runtime.clock.world_time
        next_stage = crop.stage + 1
        crop.stage = next_stage
        minutes = crop_def.stages[next_stage][0]
        crop.next_stage_at = world_time + minutes
        trace_id = payload.get("trace_id")
        runtime.event_bus.publish(
            session,
            world_time,
            "crop_grown",
            {
                "col": col,
                "row": row,
                "item_id": crop.item_id,
                "stage": next_stage,
            },
            trace_id,
        )
        if next_stage < len(crop_def.stages) - 1:
            runtime.scheduler.schedule(
                session,
                crop.planted_by,
                "crop_grow",
                crop.next_stage_at,
                {"col": col, "row": row, "stage": next_stage, "trace_id": trace_id},
            )

    # ------------------------------------------------------------------ #
    # Harvest (R23.6)
    # ------------------------------------------------------------------ #

    def harvest(
            self,
            world_id: str,
            agent_id: str,
            col: int | None,
            row: int | None,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Validate + harvest a ripe crop (R23.6)."""
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
                return False, None, MSG_BUSY  # R23.1: exclusive
            if col is None or row is None:
                return False, None, MSG_CROP_MISSING
            crop = session.get(Crop, {"world_id": world_id, "col": col, "row": row})
            if crop is None:
                return False, None, MSG_CROP_MISSING
            if abs(agent.col - col) + abs(agent.row - row) > MAX_PLANT_DISTANCE:
                return False, None, MSG_TOO_FAR
            crop_def = self.engine.crops.get(crop.item_id)
            if crop_def is None or crop.stage != len(crop_def.stages) - 1:
                return False, None, MSG_NOT_RIPE  # R23.6

            # R23.6 yield: config + held fertilizer yield_bonus (M12 C4).
            items = {
                item.item_id: item
                for item in session.scalars(
                    select(Item).where(Item.world_id == world_id)
                ).all()
            }
            yield_extra = 0
            for row_inv in session.scalars(
                    select(Inventory).where(
                        Inventory.world_id == world_id,
                        Inventory.agent_id == agent_id,
                    )
            ).all():
                item = items.get(row_inv.item_id)
                if item is not None:
                    yield_extra += item.yield_bonus * row_inv.quantity
            products: list[dict[str, Any]] = []
            for product_item_id, quantity in crop_def.yield_items:
                quantity = quantity + yield_extra
                if quantity <= 0:
                    continue
                self._add_inventory(
                    session, world_id, agent_id, product_item_id, quantity
                )
                products.append({"item_id": product_item_id, "quantity": quantity})

            item = session.get(Item, {"world_id": world_id, "item_id": crop.item_id})
            session.delete(crop)  # clears the cell; stale callbacks no-op
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "crop_harvested",
                {
                    "agent_id": agent_id,
                    "col": col,
                    "row": row,
                    "item_id": crop.item_id,
                    "item_name": item.name if item is not None else crop.item_id,
                    "products": products,
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
        session.flush()  # include rows added in this transaction
        rows = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == world_id, Inventory.agent_id == agent_id)
            .order_by(Inventory.item_id)
        ).all()
        return [{"item_id": row.item_id, "quantity": row.quantity} for row in rows]

    def log_rejection(self, world_id: str, agent_id: str, reason: str) -> None:  # pragma: no cover
        logger.debug("Crop rejected world={} agent={}: {}", world_id, agent_id, reason)
