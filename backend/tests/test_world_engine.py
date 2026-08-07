"""Engine + rules tests: seeding, clock, pathfinding, move/wait lifecycle, R1/R6/R8/R15.

These tests drive the WorldEngine directly (no HTTP, no background loop) for
deterministic clock control: advance via clock.tick(...) + engine._tick_runtime().
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.locations import WorldLocation
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.transactions import Transaction
from app.database.session import SessionLocal
from app.services.action_execution_service import (
    MSG_BUSY,
    MSG_HOTEL_UNAFFORDABLE,
    MSG_NO_DESTINATION,
    MSG_NO_PATH,
    MSG_PAUSED,
    MSG_SLEEP_NEED_HOME,
    MSG_SLEEP_NEED_HOTEL,
    ActionExecutionService,
    _line_clear,
    _smooth_path,
    find_path,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.config.gameplay import HOTEL_NIGHTLY_FEE
from app.world_engine.clock import WorldClock
from app.world_engine.engine import WorldEngine

SHOP_ANCHOR = (23, 12)
LINXIA_SPAWN = (18, 27)


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
    yield eng
    eng._runtimes.clear()


def advance_minutes(engine: WorldEngine, world_id: str, minutes: int) -> None:
    """Advance a world's clock by ``minutes`` game minutes deterministically.

    Each loop iteration resets the clock's fractional accumulator, deposits
    0.9 minutes directly, then lets the engine's own 0.1s tick cross exactly
    one boundary. Resetting the accumulator is required because TestClient
    tests run the engine's live background tick loop, which deposits 0.1s into
    the same accumulator concurrently — without the reset, a boundary can be
    crossed by the direct 0.9 deposit and never processed by the scheduler
    (the engine's own 0.1 tick then finds nothing left to cross), which made
    move/work completions flaky on the app engine.
    """
    runtime = engine.get_runtime(world_id)
    assert runtime is not None
    target = runtime.clock.world_time + minutes
    while runtime.clock.world_time < target:
        runtime.clock._accumulator = 0.0
        runtime.clock.tick(0.9)
        engine._tick_runtime(runtime)


def agent_row(engine: WorldEngine, world_id: str, agent_id: str):
    session = SessionLocal()
    try:
        return session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
    finally:
        session.close()


def place_agent(
        engine: WorldEngine, world_id: str, agent_id: str, location_id: str, col: int, row: int
) -> None:
    """Move an agent onto a location anchor (test shortcut)."""
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        agent.location_id = location_id
        agent.col = col
        agent.row = row
        session.commit()
    finally:
        session.close()


def set_agent(engine: WorldEngine, world_id: str, agent_id: str, **fields) -> None:
    """Patch live agent state fields (test shortcut)."""
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert agent is not None
        for key, value in fields.items():
            setattr(agent, key, value)
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


def test_clock_crosses_minutes() -> None:
    clock = WorldClock(world_time=480, speed=1)
    assert clock.format_time() == "08:00"
    assert clock.day == 1
    # speed 1 -> 1 game minute per real second: 0.3s crosses no boundary...
    assert clock.tick(0.3) == []
    # ...and 0.7 more seconds crosses exactly one minute.
    crossed = clock.tick(0.7)
    assert crossed == [481]
    assert clock.world_time == 481
    assert clock.format_time() == "08:01"


def test_clock_paused_frozen() -> None:
    clock = WorldClock(world_time=480, speed=5, paused=True)
    assert clock.tick(10.0) == []
    assert clock.world_time == 480
    clock.paused = False
    assert clock.tick(10.0) != []
    assert clock.world_time > 480


def test_clock_multiple_crossings() -> None:
    clock = WorldClock(world_time=479, speed=10)
    crossed = clock.tick(0.5)  # 5 game minutes
    assert crossed == [480, 481, 482, 483, 484]
    assert clock.is_day_boundary() is False
    clock.tick(96.0)
    assert clock.day == 2


def test_clock_invalid_speed() -> None:
    with pytest.raises(ValueError):
        WorldClock(speed=3)


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


def test_create_world_seeds_agents_and_locations(engine: WorldEngine) -> None:
    runtime = engine.create_world("测试村庄")
    assert runtime.world_id == "world_001"
    assert runtime.clock.world_time == 480
    assert runtime.clock.speed == 1
    assert runtime.clock.paused is False

    session = SessionLocal()
    try:
        from app.database.models.agents import Agent
        from app.database.models.locations import WorldLocation

        agents = list(
            session.query(Agent).filter(Agent.world_id == runtime.world_id).all()
        )
        locations = list(
            session.query(WorldLocation)
            .filter(WorldLocation.world_id == runtime.world_id)
            .all()
        )
    finally:
        session.close()

    assert len(agents) == 9
    by_id = {a.agent_id: a for a in agents}
    assert by_id["agent_linxia"].col == LINXIA_SPAWN[0]
    assert by_id["agent_linxia"].row == LINXIA_SPAWN[1]
    assert by_id["agent_linxia"].location_id == "linxia_home"
    assert by_id["agent_linxia"].name == "林夏"
    assert by_id["agent_linxia"].satiety == 100
    assert by_id["agent_linxia"].energy == 100
    assert by_id["agent_linxia"].loneliness == 0
    assert by_id["agent_linxia"].money == 50
    assert by_id["agent_linxia"].action_type is None

    assert len(locations) == 15
    loc_ids = {l.location_id for l in locations}
    assert "village_shop" in loc_ids and "village_plaza" in loc_ids
    assert "village_hotel" in loc_ids


# --------------------------------------------------------------------------- #
# Pathfinding
# --------------------------------------------------------------------------- #


def test_bfs_path_to_shop(world_config: ParsedWorldConfig) -> None:
    path = find_path(LINXIA_SPAWN, SHOP_ANCHOR, world_config.walkable_cells)
    assert path is not None
    assert path[0] == LINXIA_SPAWN
    assert path[-1] == SHOP_ANCHOR
    # every interior cell is walkable
    assert all(cell in world_config.walkable_cells for cell in path[1:-1])


def test_bfs_no_path(world_config: ParsedWorldConfig) -> None:
    # A cell far outside the walkable set is unreachable.
    assert find_path((0, 0), (63, 39), world_config.walkable_cells) is None


def test_limujiang_spawn_connected_to_main_network(
        world_config: ParsedWorldConfig,
) -> None:
    """Regression: spawn_limujiang (12,6) used to sit on a 12-cell island
    separated from the road network by the chenyu_home anchor — every move
    was rejected with MSG_NO_PATH. The map now bridges it at (11,6)."""
    spawn = next(s for s in world_config.spawn_points if s.spawn_id == "spawn_limujiang")
    assert (spawn.col, spawn.row) in world_config.walkable_cells
    shop = next(l for l in world_config.locations if l.location_id == "village_shop")
    path = find_path((spawn.col, spawn.row), (shop.col, shop.row), world_config.walkable_cells)
    assert path is not None, "limujiang must be able to reach the shop"
    assert (11, 6) in path  # the bridge cell is actually used


def test_bfs_path_string_pulled_on_open_grid() -> None:
    """Uniform-cost BFS zigzags along a straight run; smoothing must pull the
    waypoints into a single chord when every interior cell is walkable."""
    walkable = {(c, r) for c in range(7) for r in range(-2, 3)}
    zigzag = [(0, 0), (1, -1), (2, 0), (3, -1), (4, 0), (5, -1), (6, 0)]
    assert _smooth_path(zigzag, walkable) == [(0, 0), (6, 0)]


def test_bfs_smoothing_respects_obstacles() -> None:
    """A chord must not be pulled when its rasterized line crosses a blocked
    cell — the path keeps waypoints around the obstacle."""
    walkable = {
        (0, 0), (1, 0), (2, 0),
        (2, 1),
        (2, 2), (3, 2), (4, 2), (5, 2),
    }
    raw = [(0, 0), (1, 0), (2, 0), (2, 1), (3, 2), (4, 2), (5, 2)]
    smoothed = _smooth_path(raw, walkable)
    # (0,0)->(5,2) crosses (1,1)/(3,1) (not walkable), so no full pull;
    # (0,0)->(2,1) and (2,1)->(4,2) rasterize through walkable cells, so
    # the interior detour waypoints collapse. Every chord stays legal.
    assert smoothed == [(0, 0), (2, 1), (4, 2), (5, 2)]
    assert all(
        _line_clear(smoothed[i], smoothed[i + 1], walkable)
        for i in range(len(smoothed) - 1)
    )


# --------------------------------------------------------------------------- #
# Move lifecycle (R1/R6/R8/R15)
# --------------------------------------------------------------------------- #


def test_move_to_shop_completes(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop", reason="去买东西"
    )
    assert ok is True and reason is None
    assert envelope.type == "agent_move_started"
    assert envelope.payload["from"] == [18, 27]
    assert envelope.payload["to"] == [23, 12]
    assert envelope.payload["duration_minutes"] > 0
    assert envelope.payload["speed_multiplier"] == 1.0

    # The full (string-pulled) waypoint list is shipped so the client can
    # follow the path around obstacles; duration still derives from the raw
    # BFS step count (R6: path steps * 2 * 1.0 in clear weather).
    route = envelope.payload["path"]
    assert route[0] == [18, 27]
    assert route[-1] == [23, 12]
    assert all(
        tuple(cell) in engine.world_config.walkable_cells for cell in route[1:-1]
    )
    raw_len = len(
        find_path(LINXIA_SPAWN, SHOP_ANCHOR, engine.world_config.walkable_cells)
    )
    assert envelope.payload["duration_minutes"] == (raw_len - 1) * 2

    advance_minutes(engine, runtime.world_id, envelope.payload["ends_at"] - 480 + 1)

    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert (row.col, row.row) == SHOP_ANCHOR
    assert row.location_id == "village_shop"
    assert row.action_type is None

    envelopes = engine.events_after(runtime.world_id, 0)
    types = [e.type for e in envelopes]
    assert "agent_move_started" in types
    assert "agent_move_completed" in types


def test_snapshot_serializes_inflight_move_action(engine: WorldEngine) -> None:
    """The snapshot payload must survive an agent with an in-flight move:
    the action uses the contract key `from` (not `from_`)."""
    runtime = engine.create_world()
    ok, _, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop", reason="在路上"
    )
    assert ok is True
    payload = engine.snapshot(runtime.world_id)
    assert payload is not None
    linxia = next(a for a in payload["agents"] if a["agent_id"] == "agent_linxia")
    action = linxia["action"]
    assert action is not None
    assert action["type"] == "move"
    assert action["from"] == [18, 27]
    assert action["to"] == [23, 12]
    assert action["path"][0] == [18, 27]
    assert action["path"][-1] == [23, 12]
    assert action["reason"] == "在路上"
    assert "from_" not in action


def test_snapshot_serializes_inflight_work_action(engine: WorldEngine) -> None:
    """An in-flight work shift must surface in the snapshot with the job name
    resolved, so clients can label the task without a second lookup."""
    from app.services.economy_service import EconomyService

    engine.economy_service = EconomyService(engine, SessionLocal)
    runtime = engine.create_world()
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_farm"
        agent.col, agent.row = 47, 24
        session.commit()
    finally:
        session.close()

    ok, _, reason = engine.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干农活"
    )
    assert ok is True, reason

    payload = engine.snapshot(world_id)
    assert payload is not None
    linxia = next(a for a in payload["agents"] if a["agent_id"] == "agent_linxia")
    action = linxia["action"]
    assert action is not None
    assert action["type"] == "work"
    assert action["job_id"] == "job_farm_field"
    assert action["job_name"] == "农场劳作"
    assert action["started_at"] == payload["world"]["world_time"]
    assert action["ends_at"] == action["started_at"] + 120
    assert action["reason"] == "干农活"


def test_move_to_missing_destination_rejected(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "does_not_exist"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NO_DESTINATION


def test_delete_world_api() -> None:
    """DELETE /api/worlds/{id} removes the world and cascades all state."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/worlds", json={"name": "删除测试"})
        assert response.status_code == 201, response.text
        world_id = response.json()["world_id"]

        # children exist before the delete
        session = SessionLocal()
        try:
            assert (
                    session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
                    is not None
            )
        finally:
            session.close()

        response = client.delete(f"/api/worlds/{world_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}

        response = client.get(f"/api/worlds/{world_id}")
        assert response.status_code == 404

        session = SessionLocal()
        try:
            from app.database.models.world_events import WorldEvent

            assert (
                    session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
                    is None
            )
            assert (
                    session.scalar(
                        select(WorldEvent).where(WorldEvent.world_id == world_id)
                    )
                    is None
            )
        finally:
            session.close()

        # deleting a missing world -> 404
        response = client.delete(f"/api/worlds/{world_id}")
        assert response.status_code == 404


def test_move_with_no_path_rejected(engine: WorldEngine) -> None:
    # R6: a destination outside the walkable network is rejected with
    # MSG_NO_PATH. (0,0) is plain grass, far from any road.
    runtime = engine.create_world()
    session = SessionLocal()
    try:
        session.add(
            WorldLocation(
                world_id=runtime.world_id,
                location_id="off_road",
                name="野地",
                location_type="field",
                col=0,
                row=0,
                capacity=4,
                open_hour=0,
                close_hour=24,
            )
        )
        session.commit()
    finally:
        session.close()
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_wangfang", "off_road"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NO_PATH


def test_busy_agent_second_move_rejected(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, _, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is True
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_plaza"
    )
    assert ok is False and envelope is None
    assert reason == MSG_BUSY


def test_paused_world_rejects_move(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    found, _ = engine.set_paused(runtime.world_id, True)
    assert found is True
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is False and envelope is None
    assert reason == MSG_PAUSED

    found, _ = engine.set_paused(runtime.world_id, False)
    assert found is True
    ok, envelope, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is True and envelope is not None


def test_pause_is_idempotent(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    found, first = engine.set_paused(runtime.world_id, True)
    assert found is True and first is not None
    found, second = engine.set_paused(runtime.world_id, True)
    assert found is True and second is None  # no duplicate event


def test_control_events_persisted_no_gaps(engine: WorldEngine) -> None:
    """Pause/resume/speed events must be persisted (engine.publish commits)."""
    runtime = engine.create_world()
    engine.set_speed(runtime.world_id, 5)
    engine.set_paused(runtime.world_id, True)
    engine.set_paused(runtime.world_id, False)
    envelopes = engine.events_after(runtime.world_id, 0)
    types = [e.type for e in envelopes]
    assert "world_speed_changed" in types
    assert "world_paused" in types
    assert "world_resumed" in types
    # every allocated sequence is persisted, from 1 with no gaps
    assert [e.sequence for e in envelopes] == list(range(1, len(envelopes) + 1))


# --------------------------------------------------------------------------- #
# Wait lifecycle (R1 interruptible)
# --------------------------------------------------------------------------- #


def test_wait_completes(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_wait(
        runtime.world_id, "agent_chenyu", minutes=30, reason="歇一会儿"
    )
    assert ok is True and reason is None
    assert envelope.type == "agent_wait_started"
    assert envelope.payload["minutes"] == 30

    advance_minutes(engine, runtime.world_id, 31)
    row = agent_row(engine, runtime.world_id, "agent_chenyu")
    assert row.action_type is None
    types = [e.type for e in engine.events_after(runtime.world_id, 0)]
    assert "agent_wait_completed" in types


def test_wait_default_minutes(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, _ = engine.action_service.execute_wait(
        runtime.world_id, "agent_linxia", minutes=None
    )
    assert ok is True
    assert envelope.payload["minutes"] == 60


def test_wait_is_interruptible_by_move(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, _, _ = engine.action_service.execute_wait(
        runtime.world_id, "agent_linxia", minutes=60
    )
    assert ok is True
    # R1: wait can be interrupted -> a move replaces it.
    ok, envelope, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is True
    assert envelope.type == "agent_move_started"
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert row.action_type == "move"


# --------------------------------------------------------------------------- #
# Sleep lifecycle (R1 interruptible, R14 +40/h, sleep-place rule)
# --------------------------------------------------------------------------- #


def test_sleep_completes(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_chenyu", minutes=120, reason="睡一觉"
    )
    assert ok is True and reason is None
    assert envelope.type == "agent_sleep_started"
    assert envelope.payload["minutes"] == 120

    advance_minutes(engine, runtime.world_id, 121)
    row = agent_row(engine, runtime.world_id, "agent_chenyu")
    assert row.action_type is None
    types = [e.type for e in engine.events_after(runtime.world_id, 0)]
    assert "agent_sleep_completed" in types


def test_sleep_recovers_energy_faster_than_wait(engine: WorldEngine) -> None:
    """R14: sleep recovers energy per the global config (net of the -1/h base),
    strictly faster than wait."""
    from app.config.gameplay import (
        ENERGY_DRAIN_PER_HOUR,
        SLEEP_ENERGY_PER_HOUR,
        WAIT_ENERGY_PER_HOUR,
    )

    assert SLEEP_ENERGY_PER_HOUR > WAIT_ENERGY_PER_HOUR
    runtime = engine.create_world()
    session = SessionLocal()
    try:
        agent = session.get(
            Agent, {"world_id": runtime.world_id, "agent_id": "agent_linxia"}
        )
        agent.energy = 30
        session.commit()
    finally:
        session.close()

    ok, _, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_linxia", minutes=240
    )
    assert ok is True, reason
    advance_minutes(engine, runtime.world_id, 121)  # crosses >= 2 hour boundaries
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    expected = 30 - 2 * ENERGY_DRAIN_PER_HOUR + 2 * SLEEP_ENERGY_PER_HOUR
    assert row.energy == expected, f"expected {expected}, got {row.energy}"


def test_sleep_is_interruptible_by_move(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, _, _ = engine.action_service.execute_sleep(
        runtime.world_id, "agent_linxia", minutes=240
    )
    assert ok is True
    # R1: sleep can be interrupted -> a move replaces it.
    ok, envelope, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is True
    assert envelope.type == "agent_move_started"
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert row.action_type == "move"


def test_sleep_rejected_while_moving(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, _, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop"
    )
    assert ok is True
    ok, _, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_linxia", minutes=120
    )
    assert ok is False
    assert reason == MSG_BUSY


def test_sleep_rejected_away_from_home(engine: WorldEngine) -> None:
    """R14 sleep-place: an agent with a home may only sleep at that home."""
    runtime = engine.create_world()
    place_agent(engine, runtime.world_id, "agent_linxia", "village_shop", 23, 12)

    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_linxia", minutes=120, reason="困了"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SLEEP_NEED_HOME
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert row.action_type is None  # a rejected sleep must not start an action


def test_homeless_sleep_requires_hotel(engine: WorldEngine) -> None:
    """R14 sleep-place: homeless agents may only sleep at the hotel."""
    runtime = engine.create_world()
    # 钱多多 (agent_touzi) has no home and spawns at the plaza.
    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_touzi", minutes=120, reason="困了"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SLEEP_NEED_HOTEL

    # At the hotel the same sleep is accepted (fee charged, see below).
    place_agent(engine, runtime.world_id, "agent_touzi", "village_hotel", 37, 20)
    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_touzi", minutes=120, reason="开房睡觉"
    )
    assert ok is True and reason is None
    assert envelope.type == "agent_sleep_started"
    assert envelope.payload["place"] == "village_hotel"
    assert envelope.payload["fee"] == HOTEL_NIGHTLY_FEE


def test_hotel_sleep_charges_nightly_fee(engine: WorldEngine) -> None:
    """Hotel sleep deducts HOTEL_NIGHTLY_FEE on start (R7: no credit)."""
    runtime = engine.create_world()
    place_agent(engine, runtime.world_id, "agent_touzi", "village_hotel", 37, 20)
    set_agent(engine, runtime.world_id, "agent_touzi", money=HOTEL_NIGHTLY_FEE + 10)

    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_touzi", minutes=240, reason="住一晚"
    )
    assert ok is True and reason is None

    row = agent_row(engine, runtime.world_id, "agent_touzi")
    assert row.money == 10  # fee charged on start
    assert row.action_type == "sleep"

    session = SessionLocal()
    try:
        txs = session.query(Transaction).filter_by(
            world_id=runtime.world_id, agent_id="agent_touzi", type="hotel_fee"
        ).all()
        assert len(txs) == 1
        assert txs[0].amount == -HOTEL_NIGHTLY_FEE
    finally:
        session.close()

    money_events = [
        e
        for e in engine.events_after(runtime.world_id, 0)
        if e.type == "money_changed" and e.payload.get("agent_id") == "agent_touzi"
    ]
    assert money_events
    assert money_events[-1].payload["amount"] == -HOTEL_NIGHTLY_FEE
    assert money_events[-1].payload["reason"] == "旅店住宿费"


def test_hotel_sleep_rejected_without_money(engine: WorldEngine) -> None:
    """R7: a homeless agent without the fee cannot sleep at the hotel."""
    runtime = engine.create_world()
    place_agent(engine, runtime.world_id, "agent_touzi", "village_hotel", 37, 20)
    set_agent(engine, runtime.world_id, "agent_touzi", money=5)

    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_touzi", minutes=120, reason="开房"
    )
    assert ok is False and envelope is None
    assert reason == MSG_HOTEL_UNAFFORDABLE
    row = agent_row(engine, runtime.world_id, "agent_touzi")
    assert row.action_type is None and row.money == 5  # nothing charged


def test_rejected_sleep_keeps_existing_wait(engine: WorldEngine) -> None:
    """A rejected sleep must not destroy an in-flight wait (R1)."""
    runtime = engine.create_world()
    place_agent(engine, runtime.world_id, "agent_linxia", "village_shop", 23, 12)
    ok, _, _ = engine.action_service.execute_wait(
        runtime.world_id, "agent_linxia", minutes=60, reason="等人"
    )
    assert ok is True

    ok, envelope, reason = engine.action_service.execute_sleep(
        runtime.world_id, "agent_linxia", minutes=120, reason="困了"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SLEEP_NEED_HOME
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert row.action_type == "wait"  # the wait survives the rejected sleep


def test_observation_shows_sleep_place_guidance(engine: WorldEngine) -> None:
    """The observation tells the LLM where to sleep (home vs hotel)."""
    from app.agents.observation_service import build_observation

    runtime = engine.create_world()
    world_id = runtime.world_id
    home_obs = build_observation(
        world_id, "agent_linxia", SessionLocal, home_id="linxia_home"
    )
    assert "家: 林夏的家" in home_obs
    assert "小镇旅店" in home_obs  # sleep tool line names the hotel
    homeless_obs = build_observation(world_id, "agent_touzi", SessionLocal, home_id=None)
    assert f"无家（睡觉需去小镇旅店，每晚{HOTEL_NIGHTLY_FEE}金币）" in homeless_obs
    assert "必须去小镇旅店(village_hotel)" in homeless_obs


def test_night_low_energy_boosts_sleep_decision(engine: WorldEngine) -> None:
    """R14 night steering: idle agent with energy <= 40 gets a decision boost
    during night hours (22:00-07:00) so it goes home / to the hotel."""
    runtime = engine.create_world("夜世界", autonomous=True)
    world_id = runtime.world_id
    session = SessionLocal()
    try:
        # Drop the autonomous initial decision so linxia stays idle.
        engine.get_runtime(world_id).scheduler.cancel_for_agent(session, "agent_linxia")
        session.commit()
    finally:
        session.close()
    set_agent(engine, world_id, "agent_linxia", energy=30)

    advance_minutes(engine, world_id, 840)  # 08:00 -> 22:00 (night starts)

    session = SessionLocal()
    try:
        decides = list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == "agent_linxia",
                    ScheduledAction.action_type == "agent_decide",
                )
            ).all()
        )
    finally:
        session.close()
    assert decides, "night + low energy must schedule an agent_decide"
    assert decides[0].due_at == 1321
    assert decides[0].payload == {"origin": "needs_boost"}


# --------------------------------------------------------------------------- #
# R8: closed destination at arrival
# --------------------------------------------------------------------------- #


def test_move_completing_at_closed_destination_auto_waits(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    # Advance to 21:30 (shop closes at 20:00).
    advance_minutes(engine, runtime.world_id, 1290 - 480)
    ok, envelope, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "village_shop", reason="碰碰运气"
    )
    assert ok is True
    duration = envelope.payload["duration_minutes"]
    arrival = 1290 + duration

    advance_minutes(engine, runtime.world_id, duration + 1)

    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert (row.col, row.row) == SHOP_ANCHOR
    # R8: door wait until next open (08:00 next day = 1920).
    assert row.action_type == "wait"
    assert row.action_ends_at == 1920

    types = [e.type for e in engine.events_after(runtime.world_id, 0)]
    assert "agent_move_completed" in types
    assert "agent_wait_started" in types
    event_texts = [
        e.payload["text"]
        for e in engine.events_after(runtime.world_id, 0)
        if e.type == "world_event_created"
    ]
    assert any("还没开门" in text for text in event_texts)

    # When the open time arrives the auto-wait completes.
    advance_minutes(engine, runtime.world_id, 1920 - runtime.clock.world_time + 1)
    row = agent_row(engine, runtime.world_id, "agent_linxia")
    assert row.action_type is None


# --------------------------------------------------------------------------- #
# Event log
# --------------------------------------------------------------------------- #


def test_events_after_sequence_shape_and_order(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, _ = engine.action_service.execute_move(
        runtime.world_id, "agent_zhangming", "village_shop"
    )
    assert ok is True
    envelopes = engine.events_after(runtime.world_id, 0)
    assert envelopes
    sequences = [e.sequence for e in envelopes]
    assert sequences == sorted(sequences)
    first = envelopes[-1]  # the move_started
    assert first.type == "agent_move_started"
    assert first.event_id == f"evt_{first.sequence:06d}"
    assert first.world_id == runtime.world_id
    assert first.world_time == 480
    assert first.trace_id.startswith("trc_")
    assert isinstance(first.payload, dict)
    # after_sequence excludes everything at or below N
    tail = engine.events_after(runtime.world_id, first.sequence)
    assert all(e.sequence > first.sequence for e in tail)
