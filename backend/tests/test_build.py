"""M14 tests: build system (R22) — lifecycle, validation, connectivity,
pathfinding integration, god demolish/place, save/restore.

Drives the WorldEngine directly (no HTTP, no background loop) exactly like
test_world_engine.py: clock advanced via clock.tick + engine._tick_runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.memories import Memory
from app.database.models.structures import TileStructure
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.schemas.actions import ActionRequest
from app.services.action_execution_service import (
    MSG_NO_PATH,
    MSG_START_BLOCKED,
    ActionExecutionService,
)
from app.services.build_service import (
    MSG_BLOCKS_VILLAGE,
    MSG_BUSY,
    MSG_CELL_OCCUPIED,
    MSG_CELL_RESERVED,
    MSG_NO_MATERIALS,
    MSG_NOT_WALKABLE,
    MSG_OUT_OF_BOUNDS,
    MSG_TOO_FAR,
    BuildService,
)
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

from tests.test_world_engine import advance_minutes, agent_row

# A cell whose removal from walkable disconnects the anchor graph (probed
# from the shipped map): building a blocking structure here must be rejected.
CHOKE_CELL = (37, 24)

FENCE = "fence_wood"
HOUSE = "house_small"
FLOWER = "flower_bed"


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture()
def engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.build_service = BuildService(eng, SessionLocal)
    eng.god_action_service = GodActionService(eng, SessionLocal)
    eng.save_service = SaveService(eng, SessionLocal)
    yield eng
    eng._runtimes.clear()


def give_item(
    engine: WorldEngine, world_id: str, agent_id: str, item_id: str, quantity: int
) -> None:
    session = SessionLocal()
    try:
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
        session.commit()
    finally:
        session.close()


def place_agent(
    engine: WorldEngine, world_id: str, agent_id: str, col: int, row: int
) -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        agent.col = col
        agent.row = row
        agent.location_id = None
        agent.action_type = None
        session.commit()
    finally:
        session.close()


def structure_rows(engine: WorldEngine, world_id: str) -> list[TileStructure]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(TileStructure)
                .where(TileStructure.world_id == world_id)
                .order_by(TileStructure.col, TileStructure.row)
            ).all()
        )
    finally:
        session.close()


def held_quantity(engine: WorldEngine, world_id: str, agent_id: str, item_id: str) -> int:
    session = SessionLocal()
    try:
        row = session.get(
            Inventory,
            {"world_id": world_id, "agent_id": agent_id, "item_id": item_id},
        )
        return row.quantity if row is not None else 0
    finally:
        session.close()


def nearby_walkable(
    engine: WorldEngine, world_id: str, col: int, row: int
) -> tuple[int, int]:
    """A walkable cell within manhattan distance 3 of (col, row) that is not
    an articulation point (its removal must not disconnect the town, so a
    fence there would be legal for the lifecycle tests)."""
    walkable = engine.world_config.walkable_cells
    anchors = [(loc.col, loc.row) for loc in engine.world_config.locations] + [
        (sp.col, sp.row) for sp in engine.world_config.spawn_points
    ]
    passable = walkable | {tuple(a) for a in anchors}
    start = tuple(anchors[0])

    def is_cut(cell: tuple[int, int]) -> bool:
        seen = {start}
        frontier = [start]
        while frontier:
            c, r = frontier.pop()
            for dc in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    if dc == 0 and dr == 0:
                        continue
                    n = (c + dc, r + dr)
                    if n in seen or n == cell or n not in passable:
                        continue
                    seen.add(n)
                    frontier.append(n)
        return not all(tuple(a) in seen for a in anchors)

    for dc in range(-3, 4):
        for dr in range(-3, 4):
            cell = (col + dc, row + dr)
            if cell in walkable and not is_cut(cell):
                return cell
    raise AssertionError("no non-cut walkable cell near the target")


def nearby_footprint(
    engine: WorldEngine, world_id: str, col: int, row: int, size: int = 2
) -> tuple[int, int]:
    """Anchor whose ``size``x``size`` footprint is fully walkable and whose
    removal does not disconnect the town (house build tests)."""
    walkable = engine.world_config.walkable_cells
    anchors = [(loc.col, loc.row) for loc in engine.world_config.locations] + [
        (sp.col, sp.row) for sp in engine.world_config.spawn_points
    ]
    passable = walkable | {tuple(a) for a in anchors}
    start = tuple(anchors[0])

    def is_cut(cells: set[tuple[int, int]]) -> bool:
        seen = {start}
        frontier = [start]
        while frontier:
            c, r = frontier.pop()
            for dc in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    if dc == 0 and dr == 0:
                        continue
                    n = (c + dc, r + dr)
                    if n in seen or n in cells or n not in passable:
                        continue
                    seen.add(n)
                    frontier.append(n)
        return not all(tuple(a) in seen for a in anchors)

    for dc in range(-6, 7):
        for dr in range(-6, 7):
            anchor = (col + dc, row + dr)
            cells = {(anchor[0] + x, anchor[1] + y) for x in range(size) for y in range(size)}
            if cells <= walkable and not is_cut(cells):
                return anchor
    raise AssertionError("no fully-walkable footprint near the target")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_build_lifecycle_materials_deducted_and_built(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])

    ok, envelope, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE, reason="围起我的菜园"
    )
    assert ok, err
    assert envelope is not None and envelope.type == "build_started"
    # R22.2: materials pre-deducted, row exists as "building".
    assert held_quantity(engine, world_id, agent_id, "wood") == 4
    rows = structure_rows(engine, world_id)
    assert len(rows) == 1 and rows[0].status == "building"
    assert rows[0].blueprint_id == FENCE and rows[0].owner_agent_id == agent_id
    agent = agent_row(engine, world_id, agent_id)
    assert agent.action_type == "build"

    advance_minutes(engine, world_id, 31)
    rows = structure_rows(engine, world_id)
    assert len(rows) == 1 and rows[0].status == "built"
    assert rows[0].built_at is not None
    agent = agent_row(engine, world_id, agent_id)
    assert agent.action_type is None  # back to idle
    # structure_built event + builder memory (M6 hook).
    session = SessionLocal()
    try:
        types = list(
            session.scalars(
                select(WorldEvent.type).where(
                    WorldEvent.world_id == world_id,
                    WorldEvent.type.in_(["build_started", "structure_built"]),
                )
            )
        )
        assert types.count("build_started") == 1
        assert types.count("structure_built") == 1
        memory = session.scalars(
            select(Memory).where(
                Memory.world_id == world_id,
                Memory.agent_id == agent_id,
                Memory.memory_type == "episodic",
            )
        ).all()
        assert any("建造" in m.text for m in memory)
    finally:
        session.close()


def test_build_multi_cell_house_blocks_footprint(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 10)
    give_item(engine, world_id, agent_id, "rope", 5)
    anchor = nearby_footprint(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, anchor[0] + 1, anchor[1])
    ok, envelope, err = engine.build_service.build_start(
        world_id, agent_id, anchor[0], anchor[1], HOUSE, reason="给自己盖个家"
    )
    assert ok, err
    advance_minutes(engine, world_id, 241)
    rows = structure_rows(engine, world_id)
    assert len(rows) == 1 and rows[0].status == "built"  # anchor row only
    assert held_quantity(engine, world_id, agent_id, "wood") == 4
    assert held_quantity(engine, world_id, agent_id, "rope") == 3


# --------------------------------------------------------------------------- #
# Validation (R22.1 ~ R22.3)
# --------------------------------------------------------------------------- #


def test_build_rejected_when_busy(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        agent.action_type = "wait"
        agent.action_ends_at = 9999
        session.commit()
    finally:
        session.close()
    target = nearby_walkable(engine, world_id, 20, 25)
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert not ok and err == MSG_BUSY


def test_build_rejected_missing_materials(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert not ok and err == MSG_NO_MATERIALS


def test_build_rejected_non_walkable(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    walkable = engine.world_config.walkable_cells
    non_walkable = None
    for col in range(engine.world_config.width):
        for row in range(engine.world_config.height):
            if (col, row) not in walkable:
                non_walkable = (col, row)
                break
        if non_walkable:
            break
    place_agent(engine, world_id, agent_id, non_walkable[0] + 1, non_walkable[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, non_walkable[0], non_walkable[1], FENCE
    )
    assert not ok and err == MSG_NOT_WALKABLE


def test_build_rejected_out_of_bounds(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, -1, -1, FENCE
    )
    assert not ok and err == MSG_OUT_OF_BOUNDS


def test_build_rejected_reserved_cell(engine: WorldEngine) -> None:
    """Spawn points are walkable but reserved — no building on top of one."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    spawn = next(
        sp for sp in engine.world_config.spawn_points if sp.agent_id == "agent_linxia"
    )
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, spawn.col, spawn.row, FENCE
    )
    assert not ok and err == MSG_CELL_RESERVED


def test_build_rejected_too_far(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, 10, 10)  # far away
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert not ok and err == MSG_TOO_FAR


def test_build_rejected_occupied_cell(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert ok, err
    # A second builder (different cell, same anchor) must be rejected.
    other = "agent_zhangming"
    give_item(engine, world_id, other, "wood", 5)
    place_agent(engine, world_id, other, target[0] + 1, target[1] + 1)
    ok2, _, err2 = engine.build_service.build_start(
        world_id, other, target[0], target[1], FENCE
    )
    assert not ok2 and err2 == MSG_CELL_OCCUPIED


def test_build_house_footprint_overlap_rejected(engine: WorldEngine) -> None:
    """A house whose footprint would land on an existing fence is rejected."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 10)
    give_item(engine, world_id, agent_id, "rope", 5)
    anchor = nearby_footprint(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, anchor[0] + 1, anchor[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, anchor[0], anchor[1], FENCE
    )
    assert ok, err
    advance_minutes(engine, world_id, 31)  # fence finished, builder freed
    # A second builder tries a house whose footprint covers the fence cell.
    other = "agent_zhangming"
    give_item(engine, world_id, other, "wood", 10)
    give_item(engine, world_id, other, "rope", 5)
    house_anchor = (anchor[0] - 1, anchor[1])
    place_agent(engine, world_id, other, house_anchor[0] + 1, house_anchor[1])
    ok2, _, err2 = engine.build_service.build_start(
        world_id, other, house_anchor[0], house_anchor[1], HOUSE
    )
    assert not ok2 and err2 == MSG_CELL_OCCUPIED


# --------------------------------------------------------------------------- #
# Connectivity (R22.4)
# --------------------------------------------------------------------------- #


def test_build_blocking_at_choke_point_rejected(engine: WorldEngine) -> None:
    """A blocking structure that would disconnect the town is refused."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    place_agent(engine, world_id, agent_id, CHOKE_CELL[0] + 1, CHOKE_CELL[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, CHOKE_CELL[0], CHOKE_CELL[1], FENCE
    )
    assert not ok and err == MSG_BLOCKS_VILLAGE
    assert structure_rows(engine, world_id) == []  # nothing was placed


def test_build_non_blocking_flower_bed_at_choke_point_allowed(
    engine: WorldEngine,
) -> None:
    """Non-blocking structures never trip the connectivity invariant."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "flower_seed", 3)
    place_agent(engine, world_id, agent_id, CHOKE_CELL[0] + 1, CHOKE_CELL[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, CHOKE_CELL[0], CHOKE_CELL[1], FLOWER
    )
    assert ok, err


# --------------------------------------------------------------------------- #
# Pathfinding integration (R22.6)
# --------------------------------------------------------------------------- #


def test_effective_walkable_excludes_built_only(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    session = SessionLocal()
    try:
        # While "building": still walkable (half-built fence is no obstacle).
        engine.build_service.build_start(
            world_id, agent_id, target[0], target[1], FENCE
        )
        walkable = engine.effective_walkable(session, world_id)
        assert target in walkable
        session.commit()
    finally:
        session.close()
    advance_minutes(engine, world_id, 31)
    session = SessionLocal()
    try:
        walkable = engine.effective_walkable(session, world_id)
        assert target not in walkable  # built fence blocks the cell
    finally:
        session.close()


def test_move_reroutes_around_built_structure(engine: WorldEngine) -> None:
    """Pathfinding must not cross a blocking structure: after a fence lands
    on a farm->shop path cell, the route changes (or the move is rejected
    cleanly when no route remains)."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    farm = next(loc for loc in engine.world_config.locations if loc.location_id == "village_farm")
    shop = next(loc for loc in engine.world_config.locations if loc.location_id == "village_shop")
    start = (farm.col, farm.row)
    goal = (shop.col, shop.row)
    session = SessionLocal()
    try:
        from app.services.action_execution_service import find_path

        path = find_path(start, goal, engine.world_config.walkable_cells)
        assert path is not None, "farm and shop must be connected"
        # Pick the first cell on the path whose removal does NOT disconnect
        # the town (a legal fence spot that still forces a reroute).
        anchors = [(loc.col, loc.row) for loc in engine.world_config.locations] + [
            (sp.col, sp.row) for sp in engine.world_config.spawn_points
        ]
        passable = engine.world_config.walkable_cells | {tuple(a) for a in anchors}
        anchor0 = tuple(anchors[0])

        def is_cut(cell: tuple[int, int]) -> bool:
            seen = {anchor0}
            frontier = [anchor0]
            while frontier:
                c, r = frontier.pop()
                for dc in (-1, 0, 1):
                    for dr in (-1, 0, 1):
                        if dc == 0 and dr == 0:
                            continue
                        n = (c + dc, r + dr)
                        if n in seen or n == cell or n not in passable:
                            continue
                        seen.add(n)
                        frontier.append(n)
            return not all(tuple(a) in seen for a in anchors)

        fence_cell = next(cell for cell in path if cell in passable and not is_cut(cell))
        # Build the fence there (legal: town stays connected).
        give_item(engine, world_id, agent_id, "wood", 5)
        place_agent(engine, world_id, agent_id, fence_cell[0] + 1, fence_cell[1])
        ok, _, err = engine.build_service.build_start(
            world_id, agent_id, fence_cell[0], fence_cell[1], FENCE
        )
        assert ok, err
        session.commit()
    finally:
        session.close()
    advance_minutes(engine, world_id, 31)

    session = SessionLocal()
    try:
        walkable = engine.effective_walkable(session, world_id)
        assert fence_cell not in walkable  # built fence blocks the cell
        from app.services.action_execution_service import find_path

        new_path = find_path(start, goal, walkable)
        assert new_path is None or all(
            cell != fence_cell for cell in new_path
        ), "path crosses the built fence"
    finally:
        session.close()


def test_move_rejected_when_start_blocked(engine: WorldEngine) -> None:
    """R22.6: a structure built on the agent's own cell blocks departure."""
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0], target[1])
    # God drops a fence right on the agent's cell.
    result = engine.god_action_service.apply(
        world_id, "build_structure", target_id=None,
        parameters={"col": target[0], "row": target[1], "blueprint_id": FENCE},
        reason="测试堵人",
    )
    assert result["success"]
    result = engine.action_service.execute_action(
        world_id, agent_id, ActionRequest(
            action_type="move", destination_id="village_plaza"
        )
    )
    assert result[0] is False and result[2] == MSG_START_BLOCKED


# --------------------------------------------------------------------------- #
# God commands (R22.5, R13)
# --------------------------------------------------------------------------- #


def test_god_remove_building_refunds_materials(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert ok, err
    advance_minutes(engine, world_id, 10)  # 10 of 30 minutes elapsed
    result = engine.god_action_service.apply(
        world_id, "remove_structure",
        parameters={"col": target[0], "row": target[1]},
        reason="神谕拆除",
    )
    assert result["success"]
    # R22.2: proportional refund — 20/30 remaining -> 1 wood back (min 1).
    # 4 wood held after the pre-deduct + 1 refunded = 5.
    assert held_quantity(engine, world_id, agent_id, "wood") == 5
    assert structure_rows(engine, world_id) == []
    agent = agent_row(engine, world_id, agent_id)
    assert agent.action_type is None  # build interrupted, agent freed
    types = [e["type"] for e in result["events"]]
    assert "structure_removed" in types


def test_god_remove_built_structure(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert ok, err
    advance_minutes(engine, world_id, 31)
    result = engine.god_action_service.apply(
        world_id, "remove_structure",
        parameters={"col": target[0], "row": target[1]},
        reason="神谕拆除",
    )
    assert result["success"]
    assert structure_rows(engine, world_id) == []
    # Completed build: no refund (materials were consumed).
    assert held_quantity(engine, world_id, agent_id, "wood") == 4


def test_god_remove_missing_structure_404(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    with pytest.raises(Exception) as exc_info:
        engine.god_action_service.apply(
            runtime.world_id, "remove_structure",
            parameters={"col": 5, "row": 5},
        )
    assert exc_info.value.status_code == 404


def test_god_build_structure_places_directly(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    target = nearby_walkable(engine, world_id, 20, 25)
    result = engine.god_action_service.apply(
        world_id, "build_structure", target_id="agent_laozhang",
        parameters={"col": target[0], "row": target[1], "blueprint_id": FENCE},
        reason="神谕建造",
    )
    assert result["success"]
    rows = structure_rows(engine, world_id)
    assert len(rows) == 1 and rows[0].status == "built"
    assert rows[0].owner_agent_id == "agent_laozhang"
    assert any(e["type"] == "structure_built" for e in result["events"])


def test_god_build_structure_unknown_blueprint_400(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    with pytest.raises(Exception) as exc_info:
        engine.god_action_service.apply(
            runtime.world_id, "build_structure",
            parameters={"col": 5, "row": 5, "blueprint_id": "castle"},
        )
    assert exc_info.value.status_code == 400


# --------------------------------------------------------------------------- #
# Save / restore (R17)
# --------------------------------------------------------------------------- #


def test_save_restore_keeps_structures(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    world_id = runtime.world_id
    agent_id = "agent_linxia"
    give_item(engine, world_id, agent_id, "wood", 5)
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])
    ok, _, err = engine.build_service.build_start(
        world_id, agent_id, target[0], target[1], FENCE
    )
    assert ok, err
    advance_minutes(engine, world_id, 31)  # complete the build

    saved = engine.save_service.save(world_id)
    restored = engine.save_service.restore(saved.save_id)
    new_world_id = restored.world_id
    rows = structure_rows(engine, new_world_id)
    assert len(rows) == 1
    assert rows[0].status == "built" and rows[0].blueprint_id == FENCE
    session = SessionLocal()
    try:
        # The restored world's pathfinding treats the fence as an obstacle.
        walkable = engine.effective_walkable(session, new_world_id)
        assert target not in walkable
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Observation surface
# --------------------------------------------------------------------------- #


def test_observation_lists_blueprints(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    from app.agents.observation_service import build_observation

    observation = build_observation(runtime.world_id, "agent_linxia", SessionLocal)
    assert "【可建造的蓝图】" in observation
    assert "fence_wood" in observation and "house_small" in observation
    assert "会挡住通行" in observation  # blocking flag surfaced to the LLM


# --------------------------------------------------------------------------- #
# HTTP API path (ActionRequest -> build dispatch)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def client() -> TestClient:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_api_build_action_end_to_end(client: TestClient) -> None:
    """POST action build flows through the schema into the build service and
    the completed structure appears in the snapshot."""
    response = client.post("/api/worlds", json={"name": "API 建造"})
    assert response.status_code == 201
    world_id = response.json()["world_id"]
    agent_id = "agent_linxia"
    give_item(None, world_id, agent_id, "wood", 5)  # engine arg unused below

    from app.main import app as main_app

    engine = main_app.state.engine
    runtime = engine.get_runtime(world_id)
    assert runtime is not None
    target = nearby_walkable(engine, world_id, 20, 25)
    place_agent(engine, world_id, agent_id, target[0] + 1, target[1])

    body = {
        "action_type": "build",
        "col": target[0],
        "row": target[1],
        "blueprint_id": "fence_wood",
        "reason": "API 测试建造",
    }
    result = client.post(
        f"/api/worlds/{world_id}/agents/{agent_id}/actions", json=body
    )
    assert result.status_code == 200, result.text
    assert result.json()["event"]["type"] == "build_started"

    advance_minutes(engine, world_id, 31)
    snapshot = client.get(f"/api/worlds/{world_id}/snapshot").json()
    structures = snapshot["structures"]
    assert any(
        s["blueprint_id"] == "fence_wood" and s["status"] == "built"
        for s in structures
    )
