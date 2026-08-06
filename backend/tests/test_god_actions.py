"""M7 tests: god view & intervention system.

Drives GodActionService directly (no HTTP, no background loop) for the
command coverage, plus a TestClient block for the REST contracts
(POST /god-actions, GET /agents/{agent_id}) — the same pattern as
test_memory_relationships.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.conversations import Conversation
from app.database.models.god_actions import GodAction
from app.database.models.inventories import Inventory
from app.database.models.memories import Memory
from app.database.models.stores import StoreProduct
from app.database.models.transactions import Transaction
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.conversation_service import ConversationService
from app.services.economy_service import EconomyService
from app.services.god_action_service import GodActionService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

from tests.test_world_engine import advance_minutes

# village_plaza anchor (32,20); village_farm anchor (47,24): manhattan 19 > 3.
FARM_ANCHOR = (47, 24)
PLAZA_ANCHOR = (32, 20)

AGENTS = (
    "agent_linxia", "agent_zhangming", "agent_chenyu",
    "agent_wangfang", "agent_laozhang",
)


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def make_engine(world_config: ParsedWorldConfig) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.decision_service = DecisionService(eng, SessionLocal)
    eng.conversation_service = ConversationService(eng, SessionLocal)
    eng.economy_service = EconomyService(eng, SessionLocal)
    eng.god_action_service = GodActionService(eng, SessionLocal)
    return eng


def park_at(engine: WorldEngine, world_id: str, agent_id: str, destination_id: str) -> None:
    """Move ``agent_id`` to ``destination_id`` and wait for arrival (idle)."""
    runtime = engine.get_runtime(world_id)
    assert runtime is not None
    ok, envelope, reason = engine.action_service.execute_move(
        world_id, agent_id, destination_id, reason="测试定位"
    )
    assert ok is True, reason
    duration = envelope.payload["ends_at"] - runtime.clock.world_time
    advance_minutes(engine, world_id, duration + 1)
    session = SessionLocal()
    try:
        row = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        assert row is not None and row.action_type is None
    finally:
        session.close()


def god(engine: WorldEngine, world_id: str, **kwargs) -> dict:
    """Convenience wrapper: apply a god command and return the dict."""
    return engine.god_action_service.apply(world_id, **kwargs)


def memories_for(world_id: str, agent_id: str) -> list[Memory]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Memory)
                .where(Memory.world_id == world_id, Memory.agent_id == agent_id)
                .order_by(Memory.created_at, Memory.memory_id)
            )
        )
    finally:
        session.close()


def events_of(engine: WorldEngine, world_id: str, type_: str) -> list:
    return [e for e in engine.events_after(world_id, 0) if e.type == type_]


def audit_rows(world_id: str) -> list[GodAction]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(GodAction)
                .where(GodAction.world_id == world_id)
                .order_by(GodAction.created_at, GodAction.command_id)
            )
        )
    finally:
        session.close()


def agent_row(world_id: str, agent_id: str) -> Agent:
    session = SessionLocal()
    try:
        return session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


def test_grant_money_updates_balance_audits_and_memory(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕发钱")
    world_id = runtime.world_id

    response = god(eng, world_id, command_type="grant_money",
                   target_id="agent_linxia", parameters={"amount": 50}, reason="奖励")

    assert response["success"] is True
    assert response["command_id"].startswith("cmd_")
    assert response["result"]["balance"] == 100  # 50 + 50

    row = agent_row(world_id, "agent_linxia")
    assert row.money == 100

    session = SessionLocal()
    try:
        txs = session.scalars(
            select(Transaction).where(
                Transaction.world_id == world_id, Transaction.agent_id == "agent_linxia"
            )
        ).all()
    finally:
        session.close()
    assert len(txs) == 1
    assert txs[0].type == "god_grant"
    assert (txs[0].amount, txs[0].balance_after) == (50, 100)

    changed = events_of(eng, world_id, "money_changed")
    assert len(changed) == 1
    assert changed[0].payload["amount"] == 50
    assert changed[0].payload["balance"] == 100

    # M6 hook: god_action_applied records an episodic memory for the target.
    memory_texts = [m.text for m in memories_for(world_id, "agent_linxia")]
    assert any("神谕：获得 50 金币" in text for text in memory_texts)
    eng._runtimes.clear()


def test_deduct_money_clamps_at_zero(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕扣款")
    world_id = runtime.world_id

    response = god(eng, world_id, command_type="deduct_money",
                   target_id="agent_linxia", parameters={"amount": 200}, reason="罚款")

    assert response["success"] is True
    assert response["result"]["actual"] == 50  # clamped to balance
    assert response["result"]["balance"] == 0
    assert agent_row(world_id, "agent_linxia").money == 0

    session = SessionLocal()
    try:
        tx = session.scalars(
            select(Transaction).where(
                Transaction.world_id == world_id,
                Transaction.agent_id == "agent_linxia",
                Transaction.type == "god_deduct",
            )
        ).first()
    finally:
        session.close()
    assert tx is not None
    assert (tx.amount, tx.balance_after) == (-50, 0)

    changed = events_of(eng, world_id, "money_changed")
    assert changed[0].payload["amount"] == -50
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


def test_spawn_item_upserts_inventory(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕赐物")
    world_id = runtime.world_id

    first = god(eng, world_id, command_type="spawn_item",
                target_id="agent_linxia", parameters={"item_id": "bread", "quantity": 2})
    second = god(eng, world_id, command_type="spawn_item",
                 target_id="agent_linxia", parameters={"item_id": "bread", "quantity": 1})

    assert first["result"]["item_name"] == "面包"
    session = SessionLocal()
    try:
        inv = session.get(
            Inventory, {"world_id": world_id, "agent_id": "agent_linxia", "item_id": "bread"}
        )
    finally:
        session.close()
    assert inv is not None and inv.quantity == 3  # upsert, not overwrite

    spawned = events_of(eng, world_id, "item_spawned")
    assert len(spawned) == 2
    assert spawned[0].payload == {
        "agent_id": "agent_linxia", "item_id": "bread", "item_name": "面包", "quantity": 2,
    }
    inventory_changed = events_of(eng, world_id, "inventory_changed")
    assert len(inventory_changed) == 2
    assert {"item_id": "bread", "quantity": 3} in inventory_changed[-1].payload["items"]
    eng._runtimes.clear()


def test_spawn_unknown_item_404(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("赐物失败")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="spawn_item",
            target_id="agent_linxia", parameters={"item_id": "mystery_box"})
    assert exc.value.status_code == 404
    assert audit_rows(world_id) == []  # rejected before the audit row
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Teleport
# --------------------------------------------------------------------------- #


def test_teleport_moves_cancels_action_and_emits_events(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕传送")
    world_id = runtime.world_id

    # Start a wait so we can prove the action is cancelled.
    ok, _, reason = eng.action_service.execute_wait(
        world_id, "agent_linxia", minutes=60, reason="休息"
    )
    assert ok is True, reason

    response = god(eng, world_id, command_type="teleport",
                   target_id="agent_linxia",
                   parameters={"location_id": "village_farm"}, reason="巡视")

    assert response["success"] is True
    assert response["result"]["to"] == list(FARM_ANCHOR)

    row = agent_row(world_id, "agent_linxia")
    assert (row.col, row.row) == FARM_ANCHOR
    assert row.location_id == "village_farm"
    assert row.action_type is None  # current action cancelled

    teleports = events_of(eng, world_id, "god_teleport")
    assert len(teleports) == 1
    assert teleports[0].payload["agent_id"] == "agent_linxia"
    assert teleports[0].payload["to"] == list(FARM_ANCHOR)
    assert teleports[0].payload["reason"] == "巡视"
    assert len(events_of(eng, world_id, "agent_state_changed")) == 1
    eng._runtimes.clear()


def test_teleport_ends_far_conversation(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("传送断聊")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")

    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_tp"
    )
    assert ok is True, reason
    before = events_of(eng, world_id, "conversation_started")
    assert len(before) == 1

    god(eng, world_id, command_type="teleport", target_id="agent_linxia",
        parameters={"location_id": "village_farm"})  # 19 cells away

    session = SessionLocal()
    try:
        conv = session.scalars(
            select(Conversation).where(Conversation.world_id == world_id)
        ).first()
        assert conv is not None and conv.ended_at is not None
    finally:
        session.close()
    assert len(events_of(eng, world_id, "conversation_ended")) == 1
    eng._runtimes.clear()


def test_teleport_unknown_location_404(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("传送失败")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="teleport", target_id="agent_linxia",
            parameters={"location_id": "nowhere"})
    assert exc.value.status_code == 404
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Public events
# --------------------------------------------------------------------------- #


def test_public_event_records_memory_for_every_agent(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕公告")
    world_id = runtime.world_id

    response = god(eng, world_id, command_type="public_event",
                   parameters={"text": "神明降下了大雨，今晚大家都早点回家"})

    assert response["success"] is True
    created = events_of(eng, world_id, "world_event_created")
    assert len(created) == 1
    assert created[0].payload == {"text": "神明降下了大雨，今晚大家都早点回家", "importance": 0.8}
    assert created[0].payload.get("agent_id") is None  # public

    for agent_id in AGENTS:
        memories = memories_for(world_id, agent_id)
        assert any(
            m.memory_type == "episodic" and "神明降下了大雨" in m.text and m.importance == 0.6
            for m in memories
        ), f"{agent_id} missing the public-event memory"
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Weather
# --------------------------------------------------------------------------- #


def test_change_weather_updates_world_and_emits_event(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕天气")
    world_id = runtime.world_id
    assert agent_row(world_id, "agent_linxia") is not None  # world seeded

    response = god(eng, world_id, command_type="change_weather",
                   parameters={"weather": "rain"}, reason="测试降雨")

    assert response["result"] == {"weather": "rain"}
    session = SessionLocal()
    try:
        world = session.get(World, world_id)
        assert world.weather == "rain"
    finally:
        session.close()
    changed = events_of(eng, world_id, "weather_changed")
    assert len(changed) == 1
    assert changed[0].payload == {"weather": "rain"}
    eng._runtimes.clear()


def test_change_weather_invalid_400(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕坏天气")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="change_weather", parameters={"weather": "storm"})
    assert exc.value.status_code == 400
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Store stock
# --------------------------------------------------------------------------- #


def test_change_store_stock_sets_absolute_stock(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕库存")
    world_id = runtime.world_id

    response = god(eng, world_id, command_type="change_store_stock",
                   parameters={"item_id": "bread", "quantity": 3}, reason="补货")

    assert response["success"] is True
    assert response["result"] == {"store_id": "village_shop", "item_id": "bread", "quantity": 3}
    session = SessionLocal()
    try:
        product = session.get(
            StoreProduct,
            {"world_id": world_id, "store_id": "village_shop", "item_id": "bread"},
        )
        assert product.stock == 3
    finally:
        session.close()
    changed = events_of(eng, world_id, "store_stock_changed")
    assert len(changed) == 1
    assert changed[0].payload == {"store_id": "village_shop", "item_id": "bread", "quantity": 3}
    eng._runtimes.clear()


def test_change_store_stock_unknown_product_404(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕库存失败")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="change_store_stock",
            parameters={"item_id": "bread", "quantity": 3}, target_id="no_such_store")
    assert exc.value.status_code == 404
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Clock commands
# --------------------------------------------------------------------------- #


def test_pause_resume_and_speed_via_god(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕时钟")
    world_id = runtime.world_id

    paused = god(eng, world_id, command_type="pause")
    assert paused["success"] is True
    assert paused["result"]["paused"] is True
    assert agent_row(world_id, "agent_linxia") is not None
    session = SessionLocal()
    try:
        assert session.get(World, world_id).paused is True
    finally:
        session.close()
    assert len(events_of(eng, world_id, "world_paused")) == 1

    # Idempotent pause: still audited, but no second world_paused event.
    paused_again = god(eng, world_id, command_type="pause")
    assert paused_again["result"]["already"] is True
    assert len(events_of(eng, world_id, "world_paused")) == 1

    resumed = god(eng, world_id, command_type="resume")
    assert resumed["success"] is True
    session = SessionLocal()
    try:
        assert session.get(World, world_id).paused is False
    finally:
        session.close()
    assert len(events_of(eng, world_id, "world_resumed")) == 1

    sped = god(eng, world_id, command_type="set_speed", parameters={"speed": 5})
    assert sped["result"] == {"speed": 5}
    session = SessionLocal()
    try:
        assert session.get(World, world_id).speed == 5
    finally:
        session.close()
    changed = events_of(eng, world_id, "world_speed_changed")
    assert len(changed) == 1
    assert changed[0].payload == {"speed": 5}
    eng._runtimes.clear()


def test_set_speed_invalid_400(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕坏倍速")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="set_speed", parameters={"speed": 3})
    assert exc.value.status_code == 400
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Errors + audit trail
# --------------------------------------------------------------------------- #


def test_unknown_command_type_400(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("未知神谕")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="strike_lightning")
    assert exc.value.status_code == 400
    assert audit_rows(world_id) == []
    eng._runtimes.clear()


def test_missing_target_agent_404(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕空目标")
    world_id = runtime.world_id
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="grant_money",
            target_id="agent_nobody", parameters={"amount": 50})
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        god(eng, world_id, command_type="grant_money", parameters={"amount": 50})
    assert exc.value.status_code == 404
    eng._runtimes.clear()


def test_audit_rows_written_for_every_command(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕审计")
    world_id = runtime.world_id

    commands = [
        ("grant_money", "agent_linxia", {"amount": 50}, "奖励"),
        ("deduct_money", "agent_linxia", {"amount": 10}, "罚款"),
        ("spawn_item", "agent_linxia", {"item_id": "apple", "quantity": 2}, "赐物"),
        ("teleport", "agent_linxia", {"location_id": "village_shop"}, "传送"),
        ("public_event", None, {"text": "集市今日休市"}, "公告"),
        ("change_weather", None, {"weather": "cloudy"}, "天气"),
        ("change_store_stock", None, {"item_id": "milk", "quantity": 0}, "清空库存"),
        ("pause", None, {}, "暂停"),
        ("resume", None, {}, "恢复"),
        ("set_speed", None, {"speed": 2}, "调速"),
    ]
    for command_type, target_id, parameters, reason in commands:
        response = god(eng, world_id, command_type=command_type,
                       target_id=target_id, parameters=parameters, reason=reason)
        assert response["success"] is True

    rows = audit_rows(world_id)
    assert len(rows) == len(commands)
    by_type = {row.command_type: row for row in rows}
    assert set(by_type) == {c[0] for c in commands}
    assert by_type["grant_money"].target_id == "agent_linxia"
    assert by_type["grant_money"].parameters_json == {"amount": 50}
    assert by_type["grant_money"].reason == "奖励"
    assert by_type["grant_money"].result_json["balance"] == 100  # snapshot at grant time
    assert by_type["grant_money"].success is True
    assert all(row.success for row in rows)
    eng._runtimes.clear()


def test_god_action_applied_first_with_shared_trace_id(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕溯源")
    world_id = runtime.world_id

    response = god(eng, world_id, command_type="spawn_item",
                   target_id="agent_linxia", parameters={"item_id": "bread", "quantity": 1})

    # The returned event stream starts with god_action_applied; every event in
    # the command shares one trace_id.
    assert response["events"][0]["type"] == "god_action_applied"
    trace_ids = {event["trace_id"] for event in response["events"]}
    assert len(trace_ids) == 1
    trace_id = trace_ids.pop()
    assert trace_id.startswith("trc_")
    assert response["events"][0]["trace_id"] == trace_id

    # In the persisted log, the first event carrying that trace_id is the
    # god_action_applied envelope, and the command payload is complete.
    trace_events = [e for e in eng.events_after(world_id, 0) if e.trace_id == trace_id]
    assert len(trace_events) == 3
    assert trace_events[0].type == "god_action_applied"
    announced = trace_events[0].payload
    assert set(announced) == {
        "command_id", "command_type", "target_id", "parameters", "reason", "result",
    }
    assert announced["command_type"] == "spawn_item"
    assert announced["target_id"] == "agent_linxia"
    assert announced["parameters"] == {"item_id": "bread", "quantity": 1}
    assert announced["result"]["item_name"] == "面包"
    assert trace_events[0].sequence < trace_events[1].sequence < trace_events[2].sequence
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Agent detail API
# --------------------------------------------------------------------------- #


def test_agent_detail_contract(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕档案")
    world_id = runtime.world_id

    god(eng, world_id, command_type="grant_money",
        target_id="agent_linxia", parameters={"amount": 50})
    god(eng, world_id, command_type="spawn_item",
        target_id="agent_linxia", parameters={"item_id": "bread", "quantity": 2})

    detail = eng.agent_detail(world_id, "agent_linxia")
    assert detail is not None
    assert set(detail) == {
        "agent_id", "name", "identity", "col", "row", "location_id",
        "satiety", "energy", "mood", "money", "inventory", "action",
        "is_deciding", "consecutive_failures",
    }
    assert detail["agent_id"] == "agent_linxia"
    assert detail["name"] == "林夏"
    identity = detail["identity"]
    assert identity["id"] == "agent_linxia"
    assert identity["name"] == "林夏"
    assert identity["age"] == 24
    assert identity["occupation"] == "农场帮工"
    assert "background" in identity and "长" in identity["background"]
    assert identity["values"] == ["诚实", "稳定", "友谊"]
    assert identity["long_term_goals"] == ["存下 2000 金币", "建立自己的花圃"]
    assert identity["speaking_style"] == "温和、简短"
    assert set(identity["personality"]) == {
        "openness", "conscientiousness", "extraversion",
        "agreeableness", "emotional_stability",
    }
    assert detail["money"] == 100
    assert {"item_id": "bread", "quantity": 2} in detail["inventory"]
    assert detail["action"] is None
    assert detail["is_deciding"] is False
    assert detail["consecutive_failures"] == 0

    assert eng.agent_detail(world_id, "agent_nobody") is None
    assert eng.agent_detail("world_does_not_exist", "agent_linxia") is None
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Location detail API
# --------------------------------------------------------------------------- #


def test_location_detail_contract(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("神谕档案")
    world_id = runtime.world_id

    # Teleport 林夏 into the shop so occupants are non-empty.
    god(eng, world_id, command_type="teleport",
        target_id="agent_linxia", parameters={"location_id": "village_shop"})

    detail = eng.location_detail(world_id, "village_shop")
    assert detail is not None
    assert set(detail) == {
        "location_id", "name", "location_type", "col", "row", "capacity",
        "open_hour", "close_hour", "open", "occupants", "products", "jobs",
    }
    assert detail["name"] == "村庄杂货店"
    assert detail["location_type"] == "store"
    assert detail["capacity"] == 8
    assert detail["open"] is True  # world starts at 08:00, shop opens 08:00
    assert any(o["agent_id"] == "agent_linxia" for o in detail["occupants"])
    bread = next(p for p in detail["products"] if p["item_id"] == "bread")
    assert bread["name"] == "面包"
    assert bread["sell_price"] > 0
    assert bread["stock"] > 0
    assert any(j["job_id"] == "job_shop_attendant" for j in detail["jobs"])

    farm = eng.location_detail(world_id, "village_farm")
    assert farm is not None
    assert farm["products"] == []
    assert farm["occupants"] == []
    assert any(j["job_id"] == "job_farm_field" for j in farm["jobs"])
    assert all(set(j) == {"job_id", "name", "wage", "duration_minutes"} for j in farm["jobs"])

    assert eng.location_detail(world_id, "nowhere") is None
    assert eng.location_detail("world_does_not_exist", "village_shop") is None
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


def test_rest_god_actions_and_agent_detail(client: TestClient) -> None:
    engine = app.state.engine
    response = client.post("/api/worlds", json={"name": "API 神谕"})
    assert response.status_code == 201
    world_id = response.json()["world_id"]

    # grant_money through the HTTP contract.
    body = {
        "command_type": "grant_money",
        "target_id": "agent_linxia",
        "parameters": {"amount": 50},
        "reason": "API 奖励",
    }
    result = client.post(f"/api/worlds/{world_id}/god-actions", json=body)
    assert result.status_code == 200
    payload = result.json()
    assert payload["success"] is True
    assert payload["command_id"].startswith("cmd_")
    types = [event["type"] for event in payload["events"]]
    assert types[0] == "god_action_applied"
    assert "money_changed" in types

    # Agent detail endpoint exposes the identity card + state.
    detail = client.get(f"/api/worlds/{world_id}/agents/agent_linxia")
    assert detail.status_code == 200
    detail = detail.json()
    assert detail["identity"]["name"] == "林夏"
    assert detail["money"] == 100
    assert detail["consecutive_failures"] == 0

    # Location detail endpoint exposes occupants + store products + jobs.
    loc = client.get(f"/api/worlds/{world_id}/locations/village_shop")
    assert loc.status_code == 200
    loc = loc.json()
    assert loc["name"] == "村庄杂货店"
    assert set(loc) == {
        "location_id", "name", "location_type", "col", "row", "capacity",
        "open_hour", "close_hour", "open", "occupants", "products", "jobs",
    }
    assert any(p["item_id"] == "bread" and p["stock"] > 0 for p in loc["products"])
    assert any(j["job_id"] == "job_shop_attendant" for j in loc["jobs"])
    assert client.get(f"/api/worlds/{world_id}/locations/nowhere").status_code == 404
    assert client.get("/api/worlds/does_not_exist/locations/village_shop").status_code == 404

    # weather through the HTTP contract; weather_changed event persisted.
    weather = client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={"command_type": "change_weather", "parameters": {"weather": "rain"}, "reason": ""},
    )
    assert weather.status_code == 200
    assert weather.json()["result"] == {"weather": "rain"}

    # Audit trail reachable via the event stream (god_action_applied present).
    events = client.get(f"/api/worlds/{world_id}/events").json()
    applied = [e for e in events if e["type"] == "god_action_applied"]
    assert len(applied) == 2
    assert applied[0]["payload"]["command_type"] == "grant_money"

    # Errors: unknown command -> 400, missing agent -> 404, unknown world -> 404.
    assert client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={"command_type": "fly", "parameters": {}},
    ).status_code == 400
    assert client.post(
        f"/api/worlds/{world_id}/god-actions",
        json={"command_type": "grant_money", "target_id": "agent_nobody", "parameters": {"amount": 1}},
    ).status_code == 404
    assert client.post(
        "/api/worlds/does_not_exist/god-actions",
        json={"command_type": "pause", "parameters": {}},
    ).status_code == 404
    assert client.get("/api/worlds/does_not_exist/agents/agent_linxia").status_code == 404
    assert client.get(f"/api/worlds/{world_id}/agents/agent_nobody").status_code == 404
