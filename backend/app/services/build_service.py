"""BuildService (M14): the construction rule gate — build / complete / abort.

World rules enforced here (docs/world-rules.md):
- R1 / R22.1: build is an exclusive action (idle only, like work); god may
  interrupt it.
- R22.2: materials are pre-deducted at start (stored on the structure row);
  a god interrupt refunds proportionally to remaining time (min 1 each).
- R22.3: every footprint cell must be walkable, unoccupied (the composite
  PK (world_id, col, row) is the race guard), not a location anchor or spawn
  cell, and the anchor cell must be within manhattan distance 3 of the agent.
- R22.4: blocking blueprints require the connectivity invariant — after the
  new cells are blocked, all location anchors + spawn points must stay
  mutually reachable (BFS over effective_walkable).
- R22.5: ownership is the builder; v1 removal is god-only (god_action_service).
- R22.6: pathfinding uses effective_walkable (engine.effective_walkable),
  so built structures are real obstacles.
- R24: paving blueprints (BlueprintDef.paving) turn a non-walkable,
  non-collision cell into a walkable one; the cell joins effective_walkable
  on completion (a real shortcut for every agent) and paving placements
  skip the R22.4 connectivity check (a road can only add connectivity).

The scheduler handler ``handle_build_completed`` re-validates placement and
connectivity at completion (the world may have changed mid-build): on any
violation the build aborts with a full material refund.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.structures import TileStructure
from app.database.models.worlds import World
from app.config.gameplay import MAX_BUILD_DISTANCE
from app.services.seed_loader import BlueprintDef
from app.world_engine.engine import WorldEngine

# Rejection reasons (Chinese, surfaced in tool results / HTTP 409).
MSG_WORLD_MISSING = "世界不存在"
MSG_PAUSED = "世界已暂停"
MSG_AGENT_MISSING = "智能体不存在"
MSG_BUSY = "当前行动未完成"
MSG_BLUEPRINT_MISSING = "蓝图不存在"
MSG_OUT_OF_BOUNDS = "目标超出地图范围"
MSG_NOT_WALKABLE = "目标格不可建造"
MSG_ALREADY_WALKABLE = "目标格已经是可走的路，无需铺路"
MSG_UNPAVABLE = "目标格是障碍地形（水域/墙），无法铺路"
MSG_CELL_OCCUPIED = "该位置已被占用"
MSG_CELL_RESERVED = "该位置是建筑/出生点，不能建造"
MSG_TOO_FAR = "离目标格太远"
MSG_NO_MATERIALS = "材料不足"
MSG_BLOCKS_VILLAGE = "会堵住村庄"


class BuildService:
    """Owns the construction rule gate for all worlds (one instance, like the
    ActionExecutionService)."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Build start (R22.1 ~ R22.4)
    # ------------------------------------------------------------------ #

    def build_start(
            self,
            world_id: str,
            agent_id: str,
            col: int | None,
            row: int | None,
            blueprint_id: str | None,
            reason: str | None = None,
            trace_id: str | None = None,
    ) -> tuple[bool, Any, str | None]:
        """Validate + start a build action (R22.1~22.4)."""
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
                return False, None, MSG_BUSY  # R1/R22.1: build is exclusive
            if col is None or row is None:
                return False, None, MSG_OUT_OF_BOUNDS
            blueprint = self.engine.blueprints.get(blueprint_id or "")
            if blueprint is None:
                return False, None, MSG_BLUEPRINT_MISSING

            footprint = self._footprint_cells(blueprint, col, row)
            if not self._cells_in_bounds(footprint):
                return False, None, MSG_OUT_OF_BOUNDS
            if blueprint.paving:
                # R24: paving targets non-walkable, non-collision terrain —
                # a road only on bare ground, never on an existing path.
                if not self._cells_unpaved(footprint):
                    return False, None, MSG_ALREADY_WALKABLE
                if not self._cells_pavable(footprint):
                    return False, None, MSG_UNPAVABLE
            elif not self._cells_walkable(footprint):
                return False, None, MSG_NOT_WALKABLE
            if self._cells_reserved(session, world_id, footprint):
                return False, None, MSG_CELL_RESERVED
            if self._cells_occupied(session, world_id, footprint):
                return False, None, MSG_CELL_OCCUPIED
            if abs(agent.col - col) + abs(agent.row - row) > MAX_BUILD_DISTANCE:
                return False, None, MSG_TOO_FAR
            if not self._has_materials(session, world_id, agent_id, blueprint):
                return False, None, MSG_NO_MATERIALS
            if blueprint.blocking and not self._connectivity_ok(
                    session, world_id, footprint
            ):
                return False, None, MSG_BLOCKS_VILLAGE  # R22.4

            # R22.2: pre-deduct materials into the structure row.
            self._deduct_materials(session, world_id, agent_id, blueprint)
            ends_at = world.world_time + blueprint.duration_minutes
            structure = TileStructure(
                world_id=world_id,
                col=col,
                row=row,
                blueprint_id=blueprint.blueprint_id,
                owner_agent_id=agent_id,
                status="building",
                built_at=None,
                materials_json=dict(blueprint.materials),
            )
            session.add(structure)
            agent.action_type = "build"
            agent.action_started_at = world.world_time
            agent.action_ends_at = ends_at
            agent.action_data = {
                "blueprint_id": blueprint.blueprint_id,
                "col": col,
                "row": row,
                "reason": reason,
            }
            runtime.scheduler.schedule(
                session,
                agent_id,
                "build_completed",
                ends_at,
                {
                    "blueprint_id": blueprint.blueprint_id,
                    "col": col,
                    "row": row,
                    "trace_id": trace_id,
                },
            )
            envelope = runtime.event_bus.publish(
                session,
                world.world_time,
                "build_started",
                {
                    "agent_id": agent_id,
                    "col": col,
                    "row": row,
                    "blueprint_id": blueprint.blueprint_id,
                    "duration_minutes": blueprint.duration_minutes,
                    "ends_at": ends_at,
                    "materials": [
                        {"item_id": item_id, "quantity": quantity}
                        for item_id, quantity in blueprint.materials.items()
                    ],
                    "reason": reason,
                },
                trace_id,
            )
            session.commit()
            return True, envelope, None
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Scheduler handler (completion)
    # ------------------------------------------------------------------ #

    def handle_build_completed(self, session: Session, action: ScheduledAction) -> None:
        """Scheduler handler for "build_completed": place the structure.

        Re-validates R22.3 occupancy and R22.4 connectivity (the world may
        have changed while building); on violation the build aborts with a
        full material refund (R22.2).
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None or agent.action_type != "build":
            return  # stale or already replaced (god interrupt)
        payload = action.payload or {}
        col = int(payload.get("col") or -1)
        row = int(payload.get("row") or -1)
        blueprint = self.engine.blueprints.get(str(payload.get("blueprint_id") or ""))
        trace_id = payload.get("trace_id")
        structure = session.get(
            TileStructure, {"world_id": action.world_id, "col": col, "row": row}
        )
        world_time = runtime.clock.world_time
        if blueprint is None or structure is None or structure.status != "building":
            # Row missing (god removed it mid-build) — nothing to place.
            self.engine.action_service._clear_action(agent)
            return

        footprint = self._footprint_cells(blueprint, col, row)
        if (
                self._cells_occupied_except(session, action.world_id, footprint, col, row)
                or (blueprint.blocking and not self._connectivity_ok(session, action.world_id, footprint))
        ):
            self._abort_build(
                session, runtime, agent, structure, world_time, trace_id,
                "建造失败：位置已被占用或会堵住村庄，材料已退还",
            )
            return

        structure.status = "built"
        structure.built_at = world_time
        self.engine.action_service._clear_action(agent)
        runtime.event_bus.publish(
            session,
            world_time,
            "structure_built",
            {
                "agent_id": action.agent_id,
                "col": col,
                "row": row,
                "blueprint_id": blueprint.blueprint_id,
                "owner_agent_id": agent.agent_id,
            },
            trace_id,
        )
        # M3: autonomous worlds re-arm the LLM loop now that the build ended.
        self.engine.action_service._maybe_schedule_next_decision(session, action)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _abort_build(
            self,
            session: Session,
            runtime: Any,
            agent: Agent,
            structure: TileStructure,
            world_time: int,
            trace_id: str | None,
            text: str,
    ) -> None:
        """Refund the pre-deducted materials and remove the pending row."""
        session.delete(structure)
        self._refund_materials(
            session, agent.world_id, agent.agent_id, structure.materials_json or {}
        )
        self.engine.action_service._clear_action(agent)
        runtime.event_bus.publish(
            session,
            world_time,
            "world_event_created",
            {
                "agent_id": agent.agent_id,
                "text": text,
                "importance": "normal",
            },
            trace_id,
        )
        runtime.event_bus.publish(
            session,
            world_time,
            "inventory_changed",
            {
                "agent_id": agent.agent_id,
                "items": self._inventory_list(session, agent.world_id, agent.agent_id),
            },
            trace_id,
        )

    @staticmethod
    def _footprint_cells(blueprint: BlueprintDef, col: int, row: int) -> list[tuple[int, int]]:
        return [(col + dc, row + dr) for dc, dr in blueprint.footprint]

    def _cells_in_bounds(self, cells: list[tuple[int, int]]) -> bool:
        width = self.engine.world_config.width
        height = self.engine.world_config.height
        return all(0 <= col < width and 0 <= row < height for col, row in cells)

    def _cells_walkable(self, cells: list[tuple[int, int]]) -> bool:
        return all(cell in self.engine.world_config.walkable_cells for cell in cells)

    def _cells_unpaved(self, cells: list[tuple[int, int]]) -> bool:
        """R24: a paving target must not already be walkable (static road)."""
        return all(
            cell not in self.engine.world_config.walkable_cells for cell in cells
        )

    def _cells_pavable(self, cells: list[tuple[int, int]]) -> bool:
        """R24: a paving target must not be collision terrain (water/walls)."""
        return all(
            cell not in self.engine.world_config.collision_cells for cell in cells
        )

    def _cells_reserved(
            self, session: Session, world_id: str, cells: list[tuple[int, int]]
    ) -> bool:
        """R22.3: no location anchors or spawn points under the footprint.

        Anchors come from the DB (map-seeded + M18 runtime stall rows, single
        source), spawn points from the world config.
        """
        anchors = {
                      (loc.col, loc.row)
                      for loc in session.scalars(
                          select(WorldLocation).where(WorldLocation.world_id == world_id)
                      )
                  } | {(sp.col, sp.row) for sp in self.engine.world_config.spawn_points}
        return any(cell in anchors for cell in cells)

    @staticmethod
    def _cells_occupied(
            session: Session, world_id: str, cells: list[tuple[int, int]]
    ) -> bool:
        return BuildService._cells_occupied_except(session, world_id, cells, None, None)

    @staticmethod
    def _cells_occupied_except(
            session: Session,
            world_id: str,
            cells: list[tuple[int, int]],
            except_col: int | None,
            except_row: int | None,
    ) -> bool:
        """Any structure row on the cells (optionally ignoring one cell —
        the builder's own anchor row)."""
        for col, row in cells:
            if except_col is not None and (col, row) == (except_col, except_row):
                continue
            row_obj = session.get(
                TileStructure, {"world_id": world_id, "col": col, "row": row}
            )
            if row_obj is not None:
                return True
        return False

    def _has_materials(
            self, session: Session, world_id: str, agent_id: str, blueprint: BlueprintDef
    ) -> bool:
        for item_id, quantity in blueprint.materials.items():
            row = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if row is None or row.quantity < quantity:
                return False
        return True

    @staticmethod
    def _deduct_materials(
            session: Session, world_id: str, agent_id: str, blueprint: BlueprintDef
    ) -> None:
        for item_id, quantity in blueprint.materials.items():
            row = session.get(
                Inventory, {"world_id": world_id, "agent_id": agent_id, "item_id": item_id}
            )
            if row is None:
                continue  # validated above; defensive
            row.quantity -= quantity
            if row.quantity <= 0:
                session.delete(row)

    @staticmethod
    def _refund_materials(
            session: Session, world_id: str, agent_id: str, materials: dict[str, Any]
    ) -> None:
        for item_id, quantity in materials.items():
            quantity = int(quantity or 0)
            if quantity <= 0:
                continue
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

    def _connectivity_ok(
            self, session: Session, world_id: str, extra_blocked: list[tuple[int, int]]
    ) -> bool:
        """R22.4: after blocking ``extra_blocked``, the walkable network stays
        connected for every agent that lives in the town.

        Checked in the pure-walkable view (anchors are goal cells — reachable
        without being walkable — but they must NOT bridge components: an
        agent cannot stand on them):
          - every spawn cell stays in the main walkable component
            (a spawn isolated behind the new build = the agent can never
            leave home → rejected);
          - every location anchor keeps itself or a neighbor in that
            component (so the destination remains reachable).
        """
        blocked = set(extra_blocked)
        paved: set[tuple[int, int]] = set()
        for row in session.scalars(
                select(TileStructure).where(
                    TileStructure.world_id == world_id,
                    TileStructure.status == "built",
                )
        ).all():
            blueprint = self.engine.blueprints.get(row.blueprint_id)
            if blueprint is None:
                continue
            if blueprint.blocking:
                blocked.add((row.col, row.row))
            elif blueprint.paving:
                paved.add((row.col, row.row))
        spawns = [(sp.col, sp.row) for sp in self.engine.world_config.spawn_points]
        if not spawns:
            return True
        walkable = (self.engine.world_config.walkable_cells - blocked) | paved
        start = spawns[0]
        if start not in walkable:
            return False
        seen = {start}
        frontier: deque[tuple[int, int]] = deque([start])
        while frontier:
            col, row = frontier.popleft()
            for dcol in (-1, 0, 1):
                for drow in (-1, 0, 1):
                    if dcol == 0 and drow == 0:
                        continue
                    neighbour = (col + dcol, row + drow)
                    if neighbour in seen or neighbour not in walkable:
                        continue
                    seen.add(neighbour)
                    frontier.append(neighbour)
        if any((col, row) not in seen for col, row in spawns[1:]):
            return False
        # M18: anchors come from the DB (map-seeded + runtime stall rows), so
        # a wild-cell shop enters the invariant on open_shop and leaves it on
        # close_shop without any extra bookkeeping.
        anchors = session.scalars(
            select(WorldLocation).where(WorldLocation.world_id == world_id)
        ).all()
        for loc in anchors:
            col, row = loc.col, loc.row
            if (col, row) in seen:
                continue  # anchor itself is walkable and connected
            if any(
                (col + dcol, row + drow) in seen
                for dcol in (-1, 0, 1)
                for drow in (-1, 0, 1)
                if not (dcol == 0 and drow == 0)
            ):
                continue
            return False  # no reachable walkable cell touches the anchor
        return True

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
        logger.debug("Build rejected world={} agent={}: {}", world_id, agent_id, reason)
