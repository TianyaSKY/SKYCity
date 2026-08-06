"""M4 tests: talk tool / ConversationService (R1/R2/R9 + anti-loop), inbox
observation, priority boost, distance break, REST conversations + manual talk.

Drives WorldEngine + ConversationService + DecisionService directly (no HTTP,
no background loop) except test_manual_talk_api, which uses the TestClient.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from app.agents.observation_service import build_observation
from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.conversations import Conversation, ConversationMessage
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.conversation_service import (
    MSG_COOLDOWN,
    MSG_DUPLICATE,
    MSG_MAX_TURNS,
    MSG_NOT_NEAR,
    MSG_SENDER_BUSY,
    MSG_TARGET_BUSY,
    MSG_TARGET_MISSING,
    ConversationService,
)
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine

from tests.test_world_engine import advance_minutes

PLAZA = (32, 20)
TOWN_HALL = (29, 8)


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


def make_engine(world_config: ParsedWorldConfig, scripts=None) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.decision_service = DecisionService(
        eng, SessionLocal, provider=FakeDecisionProvider(scripts=scripts)
    )
    eng.conversation_service = ConversationService(eng, SessionLocal)
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


def conversations_for(world_id: str) -> list[Conversation]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Conversation)
                .where(Conversation.world_id == world_id)
                .order_by(Conversation.started_at)
            )
        )
    finally:
        session.close()


def messages_for(world_id: str, conversation_id: str | None = None) -> list[ConversationMessage]:
    session = SessionLocal()
    try:
        stmt = select(ConversationMessage).where(
            ConversationMessage.world_id == world_id
        )
        if conversation_id is not None:
            stmt = stmt.where(
                ConversationMessage.conversation_id == conversation_id
            )
        return list(session.scalars(stmt.order_by(ConversationMessage.sent_at)))
    finally:
        session.close()


def world_time(world_id: str) -> int:
    session = SessionLocal()
    try:
        return int(
            session.scalar(select(World.world_time).where(World.world_id == world_id)) or 0
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Validation + lifecycle (service level)
# --------------------------------------------------------------------------- #


def test_talk_validation(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("对话校验")
    world_id = runtime.world_id
    # linxia + zhangming at the plaza, wangfang stays at her distant spawn.
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    # nearby idle pair -> success, conversation_message event + row stored
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_t1"
    )
    assert ok is True and reason is None
    assert envelope is not None and envelope.type == "conversation_message"
    assert envelope.payload["from_agent_id"] == "agent_linxia"
    assert envelope.payload["message"] == "你好呀"
    rows = messages_for(world_id)
    assert len(rows) == 1
    assert rows[0].read is False
    convo = conversations_for(world_id)[0]
    assert convo.turns == 1
    event_types = [e.type for e in eng.events_after(world_id, 0)]
    assert "conversation_started" in event_types
    assert "conversation_message" in event_types

    # far (wangfang at spawn, distance > 3) -> rejected
    ok, reason, envelope = service.send_message(
        world_id, "agent_wangfang", "agent_linxia", "你好", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_NOT_NEAR

    # unknown target -> rejected
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_ghost", "你好", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_TARGET_MISSING

    # busy target -> rejected
    assert eng.action_service.execute_wait(
        world_id, "agent_zhangming", minutes=30, reason="忙"
    )[0] is True
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "在吗", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_TARGET_BUSY

    # busy sender -> rejected
    assert eng.action_service.execute_wait(
        world_id, "agent_linxia", minutes=30, reason="忙"
    )[0] is True
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "在吗", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SENDER_BUSY
    eng._runtimes.clear()


def test_priority_boost(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("优先级提升")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    def boosted_decides() -> list[ScheduledAction]:
        session = SessionLocal()
        try:
            return list(
                session.scalars(
                    select(ScheduledAction).where(
                        ScheduledAction.world_id == world_id,
                        ScheduledAction.agent_id == "agent_zhangming",
                        ScheduledAction.action_type == "agent_decide",
                        ScheduledAction.due_at == world_time(world_id) + 1,
                    )
                )
            )
        finally:
            session.close()

    # non-autonomous world: no boost
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好", "greet"
    )
    assert ok is True and reason is None
    assert boosted_decides() == []

    # autonomous world: idle target -> agent_decide at world_time + 1
    eng.set_autonomous(world_id, True)
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True and reason is None
    boosted = boosted_decides()
    assert len(boosted) == 1, "exactly one boost scheduled at +1"
    assert boosted[0].payload.get("origin") == "conversation_boost"

    # a second message does not stack another +1 decision
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "在吗", "chat"
    )
    assert ok is True
    assert len(boosted_decides()) == 1
    eng._runtimes.clear()


def test_talk_relieves_loneliness(world_config: ParsedWorldConfig) -> None:
    """R21: a delivered talk message cuts loneliness for both parties and
    publishes needs_changed with the relieved value."""
    eng = make_engine(world_config)
    runtime = eng.create_world("孤单缓解")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    session = SessionLocal()
    try:
        for agent_id in ("agent_linxia", "agent_zhangming"):
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            agent.loneliness = 50
        session.commit()
    finally:
        session.close()

    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀，今天天气不错", "greet"
    )
    assert ok is True and reason is None

    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        zhangming = session.get(Agent, {"world_id": world_id, "agent_id": "agent_zhangming"})
        assert linxia.loneliness == 40  # both relieved by LONELINESS_RELIEF
        assert zhangming.loneliness == 40
    finally:
        session.close()

    events = eng.events_after(world_id, 0)
    for agent_id in ("agent_linxia", "agent_zhangming"):
        assert any(
            e.type == "needs_changed"
            and e.payload["agent_id"] == agent_id
            and e.payload["loneliness"] == 40
            for e in events
        ), f"needs_changed must carry the relieved loneliness for {agent_id}"
    eng._runtimes.clear()


def test_max_turns_and_cooldown(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("最大轮数与冷却")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    # 6 messages all accepted
    for i in range(6):
        ok, reason, envelope = service.send_message(
            world_id,
            "agent_linxia" if i % 2 == 0 else "agent_zhangming",
            "agent_zhangming" if i % 2 == 0 else "agent_linxia",
            f"消息{i + 1}",
            "chat",
        )
        assert ok is True, f"message {i + 1}: {reason}"
    convo = conversations_for(world_id)[0]
    assert convo.turns == 6
    assert convo.ended_at is None

    # the 7th message is rejected and ends the conversation (max_turns)
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "消息7", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_MAX_TURNS
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "max_turns"
    ended = [e for e in eng.events_after(world_id, 0) if e.type == "conversation_ended"]
    assert ended and ended[0].payload["reason"] == "max_turns"

    # pair is in cooldown: new talk rejected within 60 minutes
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "再来", "greet"
    )
    assert ok is False and envelope is None
    assert reason == MSG_COOLDOWN

    # after the cooldown window a new conversation is allowed
    advance_minutes(eng, world_id, 61)
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "好久不见", "greet"
    )
    assert ok is True and envelope is not None
    assert len(conversations_for(world_id)) == 2
    eng._runtimes.clear()


def test_duplicate_ends(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("重复结束")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True
    ok, reason, _ = service.send_message(
        world_id, "agent_zhangming", "agent_linxia", "嗨", "chat"
    )
    assert ok is True

    # the exact same (sender, message) again -> conversation ends
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is False and envelope is None
    assert reason == MSG_DUPLICATE
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "duplicate"
    ended = [e for e in eng.events_after(world_id, 0) if e.type == "conversation_ended"]
    assert ended and ended[0].payload["reason"] == "duplicate"
    eng._runtimes.clear()


def test_distance_break(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("距离打断")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    park_at(eng, world_id, "agent_chenyu", "village_plaza")
    service = eng.conversation_service

    ok, reason, _ = service.send_message(
        world_id, "agent_chenyu", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True and reason is None
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is None

    # chenyu walks to the town hall (> 3 cells from the plaza)
    assert (
        eng.action_service.execute_move(
            world_id, "agent_chenyu", "town_hall", reason="去镇公所"
        )[0]
        is True
    )
    advance_minutes(eng, world_id, 60)

    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "distance"
    ended = [e for e in eng.events_after(world_id, 0) if e.type == "conversation_ended"]
    assert ended and ended[0].payload["reason"] == "distance"
    eng._runtimes.clear()


def test_observation_contains_messages(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("观察收件箱")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")

    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀，今天天气不错", "greet"
    )
    assert ok is True and reason is None

    first = build_observation(world_id, "agent_zhangming", SessionLocal)
    assert "【收到的消息】" in first
    assert "林夏" in first
    assert "你好呀，今天天气不错" in first

    # after the observation the message is marked read and no longer shown
    second = build_observation(world_id, "agent_zhangming", SessionLocal)
    assert "你好呀，今天天气不错" not in second
    rows = messages_for(world_id)
    assert len(rows) == 1 and rows[0].read is True
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Autonomous reply flow (fake provider demo)
# --------------------------------------------------------------------------- #


def test_reply_flow_via_fake_provider(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("对话演示")
    world_id = runtime.world_id
    # park the pair at the plaza first, then let the decision loop run
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    park_at(eng, world_id, "agent_chenyu", "village_plaza")
    # M10: the new investor spawns at the plaza; park him out of earshot so
    # the deterministic reply-flow demo still features the intended pair.
    park_at(eng, world_id, "agent_touzi", "village_farm")
    eng.set_autonomous(world_id, True)

    advance_minutes(eng, world_id, 15)

    rows = messages_for(world_id)
    assert len(rows) >= 2, "the pair exchanged at least two messages"
    convo = conversations_for(world_id)[0]
    assert convo.turns >= 2
    assert convo.ended_at is not None, "conversation ended via leave"
    assert convo.end_reason == "leave"
    intents = [m.intent for m in rows]
    assert "greet" in intents and "leave" in intents
    event_types = [e.type for e in eng.events_after(world_id, 0)]
    assert "conversation_started" in event_types
    assert "conversation_message" in event_types
    assert "conversation_ended" in event_types
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# REST: manual talk action + conversations history
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


def _teleport(world_id: str, agent_id: str, col: int, row: int, location_id: str) -> None:
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        agent.col = col
        agent.row = row
        agent.location_id = location_id
        session.commit()
    finally:
        session.close()


def test_manual_talk_api(client: TestClient) -> None:
    response = client.post("/api/worlds", json={"name": "对话API"})
    assert response.status_code == 201, response.text
    world_id = response.json()["world_id"]

    # teleport linxia + zhangming to the plaza, wangfang stays far away
    _teleport(world_id, "agent_linxia", *PLAZA, "village_plaza")
    _teleport(world_id, "agent_zhangming", *PLAZA, "village_plaza")

    # nearby talk -> 200 with the conversation_message event
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={
            "action_type": "talk",
            "target_agent_id": "agent_zhangming",
            "message": "你好呀",
            "intent": "greet",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["event"]["type"] == "conversation_message"
    assert body["event"]["payload"]["to_agent_id"] == "agent_zhangming"

    # bad distance -> 409
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_wangfang/actions",
        json={
            "action_type": "talk",
            "target_agent_id": "agent_linxia",
            "message": "在吗",
            "intent": "chat",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"success": False, "reason": MSG_NOT_NEAR}

    # missing message -> 422 (schema)
    response = client.post(
        f"/api/worlds/{world_id}/agents/agent_linxia/actions",
        json={"action_type": "talk", "target_agent_id": "agent_zhangming"},
    )
    assert response.status_code == 422

    # history endpoint shows the conversation with its message
    response = client.get(
        f"/api/worlds/{world_id}/agents/agent_linxia/conversations?limit=20"
    )
    assert response.status_code == 200, response.text
    history = response.json()
    assert len(history) == 1
    item = history[0]
    assert item["other_agent_id"] == "agent_zhangming"
    assert item["ended_at"] is None and item["end_reason"] is None
    assert item["messages"][0]["message"] == "你好呀"
    assert item["messages"][0]["intent"] == "greet"
