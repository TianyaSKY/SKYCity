"""Engine + rules tests: seeding, clock, pathfinding, move/wait lifecycle, R1/R6/R8/R15.

These tests drive the WorldEngine directly (no HTTP, no background loop) for
deterministic clock control: advance via clock.tick(...) + engine._tick_runtime().
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.session import SessionLocal
from app.services.action_execution_service import (
    MSG_BUSY,
    MSG_NO_DESTINATION,
    MSG_NO_PATH,
    MSG_PAUSED,
    ActionExecutionService,
    find_path,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
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

    Each loop iteration deposits 0.9 minutes directly then lets the engine's
    own 0.1s tick cross the remaining boundary, so the scheduler fires exactly
    once per minute.
    """
    runtime = engine.get_runtime(world_id)
    assert runtime is not None
    target = runtime.clock.world_time + minutes
    while runtime.clock.world_time < target:
        runtime.clock.tick(0.9)
        engine._tick_runtime(runtime)


def agent_row(engine: WorldEngine, world_id: str, agent_id: str):
    session = SessionLocal()
    try:
        return session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
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

    assert len(agents) == 5
    by_id = {a.agent_id: a for a in agents}
    assert by_id["agent_linxia"].col == LINXIA_SPAWN[0]
    assert by_id["agent_linxia"].row == LINXIA_SPAWN[1]
    assert by_id["agent_linxia"].location_id == "linxia_home"
    assert by_id["agent_linxia"].name == "林夏"
    assert by_id["agent_linxia"].hunger == 0
    assert by_id["agent_linxia"].energy == 100
    assert by_id["agent_linxia"].money == 50
    assert by_id["agent_linxia"].action_type is None

    assert len(locations) == 8
    loc_ids = {l.location_id for l in locations}
    assert "village_shop" in loc_ids and "village_plaza" in loc_ids


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

    # R6: duration = path steps * 2 * 1.0 (clear weather).
    path_len = len(
        find_path(LINXIA_SPAWN, SHOP_ANCHOR, engine.world_config.walkable_cells)
    )
    assert envelope.payload["duration_minutes"] == (path_len - 1) * 2

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
    assert action["reason"] == "在路上"
    assert "from_" not in action


def test_move_to_missing_destination_rejected(engine: WorldEngine) -> None:
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_linxia", "does_not_exist"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NO_DESTINATION


def test_move_with_no_path_rejected(engine: WorldEngine) -> None:
    # agent_wangfang spawns in the farm compound, which the map's walkable
    # network does not connect to the rest of the town (R6: 无可行路径).
    runtime = engine.create_world()
    ok, envelope, reason = engine.action_service.execute_move(
        runtime.world_id, "agent_wangfang", "village_shop"
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
