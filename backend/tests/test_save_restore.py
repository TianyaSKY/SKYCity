"""M9 tests: save/restore/replay.

Covers the full-state roundtrip (rich world -> save -> restore into a NEW
world), event-sequence continuity across the save boundary, autonomous
continuation after restore, engine restart (load_existing), the replay
payload, the save listing API and the missing-save 404.

Direct-engine tests drive the WorldEngine synchronously (no background tick
loop), exactly like test_world_engine.py; the API tests use the TestClient.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.inventories import Inventory
from app.database.models.llm_runs import LLMRun
from app.database.models.memories import Memory
from app.database.models.relationships import Relationship
from app.database.models.saves import Save
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.stores import StoreProduct
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.conversation_service import ConversationService
from app.services.economy_service import EconomyService
from app.services.god_action_service import GodActionService
from app.services.save_service import SaveService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

from tests.test_world_engine import advance_minutes

BREAD = "bread"
PLAZA = "village_plaza"
SHOP = "village_shop"


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def make_engine(world_config: ParsedWorldConfig) -> WorldEngine:
    """Full wiring (action/economy/decision/conversation/god/save services)."""
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    eng.decision_service = DecisionService(
        eng, SessionLocal, provider=FakeDecisionProvider()
    )
    eng.conversation_service = ConversationService(eng, SessionLocal)
    eng.god_action_service = GodActionService(eng, SessionLocal)
    eng.save_service = SaveService(eng, SessionLocal)
    return eng


def world_state(world_id: str) -> dict:
    """Compact comparable snapshot of one world's DB rows."""
    session = SessionLocal()
    try:
        world = session.get(World, world_id)
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id).order_by(Agent.agent_id)
        ).all()
        inventories = session.scalars(
            select(Inventory)
            .where(Inventory.world_id == world_id)
            .order_by(Inventory.agent_id, Inventory.item_id)
        ).all()
        bread_stock = session.scalar(
            select(StoreProduct.stock).where(
                StoreProduct.world_id == world_id,
                StoreProduct.store_id == "village_shop",
                StoreProduct.item_id == BREAD,
            )
        )
        relationships = session.scalars(
            select(Relationship).where(Relationship.world_id == world_id)
        ).all()
        memories = session.scalars(
            select(Memory).where(Memory.world_id == world_id)
        ).all()
        llm_runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        scheduled = session.scalars(
            select(ScheduledAction).where(ScheduledAction.world_id == world_id)
        ).all()
        return {
            "world_time": world.world_time if world is not None else None,
            "agents": {
                agent.agent_id: {
                    "col": agent.col,
                    "row": agent.row,
                    "satiety": agent.satiety,
                    "energy": agent.energy,
                    "money": agent.money,
                    "action_type": agent.action_type,
                }
                for agent in agents
            },
            "inventories": [
                (row.agent_id, row.item_id, row.quantity) for row in inventories
            ],
            "bread_stock": bread_stock,
            "relationships": [
                (
                    row.source_agent_id,
                    row.target_agent_id,
                    row.familiarity,
                    row.trust,
                    row.affection,
                    row.resentment,
                    row.debt,
                )
                for row in relationships
            ],
            "memories": len(memories),
            "llm_runs": len(llm_runs),
            "scheduled_actions": len(scheduled),
            "pending_decides": sum(
                1 for row in scheduled if row.action_type == "agent_decide"
            ),
        }
    finally:
        session.close()


def _wait_until_idle(
    client: TestClient, world_id: str, agent_id: str, timeout: float = 20.0
) -> None:
    """Poll the agent detail endpoint until the agent has no in-flight action."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/worlds/{world_id}/agents/{agent_id}").json()
        if detail.get("action") is None:
            return
        time.sleep(0.2)
    pytest.fail(f"agent {agent_id} never became idle in world {world_id}")


# --------------------------------------------------------------------------- #
# Roundtrip: rich world -> save -> restore into a NEW world
# --------------------------------------------------------------------------- #


def test_save_roundtrip_preserves_state(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("存档世界", autonomous=True)
    world_id = runtime.world_id

    # ~30 game minutes of autonomous life: scripted moves / buys / waits fire.
    advance_minutes(eng, world_id, 30)
    # Guarantee observable economy, memory and relationship state.
    eng.god_action_service.apply(
        world_id, "grant_money", "agent_linxia", {"amount": 50}, "奖励"
    )
    eng.god_action_service.apply(
        world_id, "teleport", "agent_linxia", {"location_id": PLAZA}, "传送"
    )
    eng.god_action_service.apply(
        world_id, "teleport", "agent_zhangming", {"location_id": PLAZA}, "传送"
    )
    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀，广场真热闹", "greet"
    )
    assert ok, reason

    before = world_state(world_id)
    assert before["llm_runs"] > 0, "the autonomous advance must produce decisions"
    assert before["relationships"], "the talk must produce relationship rows"
    assert before["memories"] > 0

    result = eng.save_service.save(world_id)
    restored = eng.save_service.restore(result.save_id)
    new_id = restored.world_id
    assert new_id != world_id

    after = world_state(new_id)
    assert after["world_time"] == before["world_time"]
    assert after["agents"] == before["agents"]  # position/needs/money per agent
    assert after["inventories"] == before["inventories"]
    assert after["bread_stock"] == before["bread_stock"]
    # Relationships restored verbatim (source/target/axes), not empty.
    assert after["relationships"] == before["relationships"]
    # Memory set identical (the restore announcement adds no memories).
    assert after["memories"] == before["memories"]
    # llm_runs re-pointed at the new world, same count.
    assert after["llm_runs"] == before["llm_runs"]
    # Pending scheduled actions (incl. agent_decide) restored.
    assert after["scheduled_actions"] > 0
    assert after["pending_decides"] > 0
    eng._runtimes.clear()


def test_restore_autonomous_continues(world_config: ParsedWorldConfig) -> None:
    """A restored autonomous world keeps deciding: new llm_runs appear."""
    eng = make_engine(world_config)
    runtime = eng.create_world("自主恢复", autonomous=True)
    world_id = runtime.world_id
    advance_minutes(eng, world_id, 10)
    result = eng.save_service.save(world_id)
    restored = eng.save_service.restore(result.save_id)
    new_id = restored.world_id

    session = SessionLocal()
    try:
        before = len(
            session.scalars(select(LLMRun).where(LLMRun.world_id == new_id)).all()
        )
    finally:
        session.close()
    assert before > 0  # saved runs were re-pointed at the new world

    advance_minutes(eng, new_id, 6)  # initial decisions were re-armed at +2..+6
    session = SessionLocal()
    try:
        after = len(
            session.scalars(select(LLMRun).where(LLMRun.world_id == new_id)).all()
        )
    finally:
        session.close()
    assert after > before
    eng._runtimes.clear()


def test_restart_simulation(world_config: ParsedWorldConfig) -> None:
    """A fresh engine + load_existing picks up the restored world and ticks."""
    eng1 = make_engine(world_config)
    runtime = eng1.create_world("重启世界", autonomous=True)
    world_id = runtime.world_id
    advance_minutes(eng1, world_id, 5)
    result = eng1.save_service.save(world_id)
    saved_max = result_saved_max(result.save_id)
    restored = eng1.save_service.restore(result.save_id)
    new_id = restored.world_id
    eng1._runtimes.clear()

    eng2 = make_engine(world_config)
    eng2.load_existing()
    runtime2 = eng2.get_runtime(new_id)
    assert runtime2 is not None
    t0 = runtime2.clock.world_time
    advance_minutes(eng2, new_id, 2)
    assert runtime2.clock.world_time > t0

    session = SessionLocal()
    try:
        max_seq = session.scalar(
            select(func.max(WorldEvent.sequence)).where(WorldEvent.world_id == new_id)
        )
    finally:
        session.close()
    assert max_seq is not None and max_seq > saved_max
    eng2._runtimes.clear()


def result_saved_max(save_id: str) -> int:
    session = SessionLocal()
    try:
        row = session.get(Save, save_id)
        assert row is not None
        return int(row.payload_json["max_sequence"])
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# REST contracts
# --------------------------------------------------------------------------- #


def test_restore_continues_sequence(client: TestClient) -> None:
    """The restored world's event stream continues the saved max + 1."""
    created = client.post("/api/worlds", json={}).json()
    world_id = created["world_id"]

    # A short wait gives the original world a real event stream.
    assert (
        client.post(
            f"/api/worlds/{world_id}/agents/agent_linxia/actions",
            json={"action_type": "wait", "minutes": 1, "reason": "测试等待"},
        ).status_code
        == 200
    )
    _wait_until_idle(client, world_id, "agent_linxia")

    saved = client.post(f"/api/worlds/{world_id}/save").json()
    assert saved["save_id"].startswith("save_")
    saved_max = result_saved_max(saved["save_id"])
    assert saved_max >= 2  # wait_started + wait_completed (+ time ticks)

    restored = client.post(
        "/api/worlds/restore", json={"save_id": saved["save_id"]}
    ).json()
    new_id = restored["world_id"]
    assert new_id != world_id
    assert restored["save_id"] == saved["save_id"]
    assert restored["world_time"] == saved["created_at"]
    assert restored["paused"] is False
    assert restored["autonomous"] is False

    events = client.get(f"/api/worlds/{new_id}/events?after_sequence=0").json()
    # The restored world carries the FULL history (re-pointed from the save):
    # contiguous from 1, with the continuation beyond the saved max.
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert sequences[-1] > saved_max

    # A move via the actions API continues the restored stream.
    response = client.post(
        f"/api/worlds/{new_id}/agents/agent_linxia/actions",
        json={"action_type": "move", "destination_id": SHOP, "reason": "恢复后继续"},
    )
    assert response.status_code == 200, response.text
    events_after = client.get(f"/api/worlds/{new_id}/events?after_sequence=0").json()
    sequences = [event["sequence"] for event in events_after]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))
    assert sequences[-1] > saved_max
    assert any(
        event["type"] == "agent_move_started"
        and len(event["payload"].get("to") or []) == 2  # destination cell
        for event in events_after
    )


def test_replay_endpoint(client: TestClient) -> None:
    """Replay: snapshot present, events contiguous from sequence 1, sorted."""
    created = client.post("/api/worlds", json={}).json()
    world_id = created["world_id"]
    assert (
        client.post(
            f"/api/worlds/{world_id}/agents/agent_linxia/actions",
            json={"action_type": "wait", "minutes": 1, "reason": "重放测试"},
        ).status_code
        == 200
    )
    _wait_until_idle(client, world_id, "agent_linxia")

    replay = client.get(f"/api/worlds/{world_id}/replay").json()
    assert replay["world_id"] == world_id
    snapshot = replay["initial_snapshot"]
    assert set(snapshot["world"]) >= {
        "world_id", "world_time", "speed", "paused", "weather", "day",
    }
    assert len(snapshot["agents"]) == 6
    events = replay["events"]
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))
    assert any(event["type"] == "agent_wait_started" for event in events)


def test_save_list_endpoint(client: TestClient) -> None:
    created = client.post("/api/worlds", json={}).json()
    world_id = created["world_id"]
    saved = client.post(f"/api/worlds/{world_id}/save").json()

    listed = client.get("/api/saves").json()
    assert any(
        item["save_id"] == saved["save_id"]
        and item["world_id"] == world_id
        and item["created_at"] == saved["created_at"]
        for item in listed
    )
    filtered = client.get(f"/api/saves?world_id={world_id}").json()
    assert [item["save_id"] for item in filtered] == [saved["save_id"]]
    # newest first
    created_at = [item["created_at"] for item in filtered]
    assert created_at == sorted(created_at, reverse=True)


def test_restore_missing_save_404(client: TestClient) -> None:
    response = client.post(
        "/api/worlds/restore", json={"save_id": "save_nonexistent"}
    )
    assert response.status_code == 404

    # Saving an unknown world also 404s.
    assert client.post("/api/worlds/world_999/save").status_code == 404
    assert client.get("/api/worlds/world_999/replay").status_code == 404
