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
    MSG_QUEUED,
    MSG_SENDER_BUSY,
    MSG_TARGET_BUSY,
    MSG_TARGET_MISSING,
    ConversationService,
)
from app.config.gameplay import TALK_LOCK_SECONDS, TALK_REPLY_GRACE
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

    # let both waits finish, then the nearby idle pair can talk
    advance_minutes(eng, world_id, 31)

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

    # E-full: the conversation locks both members with a hard cap
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        zhangming = session.get(Agent, {"world_id": world_id, "agent_id": "agent_zhangming"})
        assert linxia.action_type == "talk"
        assert zhangming.action_type == "talk"
        assert linxia.action_data["conversation_id"] == convo.conversation_id
        assert linxia.action_ends_at == world_time(world_id) + TALK_LOCK_SECONDS
    finally:
        session.close()

    # in-conversation replies still work (the lock is not busy for itself)
    ok, reason, envelope = service.send_message(
        world_id, "agent_zhangming", "agent_linxia", "嗨", "chat"
    )
    assert ok is True and reason is None
    assert len(messages_for(world_id)) == 2

    # E-full: a locked member is busy for third parties
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_wangfang", "你好", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_SENDER_BUSY
    ok, reason, envelope = service.send_message(
        world_id, "agent_wangfang", "agent_linxia", "你好", "chat"
    )
    assert ok is False and envelope is None
    assert reason == MSG_TARGET_BUSY

    # leave ends the conversation and unlocks both members
    ok, reason, envelope = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "再见", "leave"
    )
    assert ok is True and reason is None
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "leave"
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        zhangming = session.get(Agent, {"world_id": world_id, "agent_id": "agent_zhangming"})
        assert linxia.action_type is None
        assert zhangming.action_type is None
    finally:
        session.close()
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

    from app.config.gameplay import LONELINESS_RELIEF

    relieved = 50 - LONELINESS_RELIEF
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        zhangming = session.get(Agent, {"world_id": world_id, "agent_id": "agent_zhangming"})
        assert linxia.loneliness == relieved  # both relieved by LONELINESS_RELIEF
        assert zhangming.loneliness == relieved
    finally:
        session.close()

    events = eng.events_after(world_id, 0)
    for agent_id in ("agent_linxia", "agent_zhangming"):
        assert any(
            e.type == "needs_changed"
            and e.payload["agent_id"] == agent_id
            and e.payload["loneliness"] == relieved
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
    """R9: moving out of earshot ends the conversation.

    E-full: a locked mover cannot walk (the move is queued), so the distance
    end is exercised through the active-but-unlocked state a god interrupt
    leaves behind — the lock is cleared, the conversation stays open, and the
    mover walking away ends it at arrival.
    """
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

    # E-full: while locked the move is queued, not executed
    ok, envelope, reason = eng.action_service.execute_move(
        world_id, "agent_chenyu", "village_hotel", reason="去旅店"
    )
    assert ok is True and envelope is None
    assert reason == MSG_QUEUED
    session = SessionLocal()
    try:
        chenyu = session.get(Agent, {"world_id": world_id, "agent_id": "agent_chenyu"})
        assert chenyu.action_type == "talk"  # still locked, position unchanged
        queued = list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == "agent_chenyu",
                    ScheduledAction.action_type == "queued_action",
                )
            )
        )
        assert len(queued) == 1 and queued[0].payload["tool"] == "move"
    finally:
        session.close()

    # god-style interrupt: clear the lock, conversation stays active
    session = SessionLocal()
    try:
        chenyu = session.get(Agent, {"world_id": world_id, "agent_id": "agent_chenyu"})
        chenyu.action_type = None
        chenyu.action_started_at = None
        chenyu.action_ends_at = None
        chenyu.action_data = None
        session.commit()
    finally:
        session.close()

    # chenyu walks to the hotel (5 cells, 10 min < the 15-min lock cap, but
    # > 3 cells from the plaza) -> arrival ends the conversation by distance
    assert (
            eng.action_service.execute_move(
                world_id, "agent_chenyu", "village_hotel", reason="去旅店"
            )[0]
            is True
    )
    advance_minutes(eng, world_id, 11)

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

    # bad distance -> 409 (before any conversation: wangfang far, both idle)
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

    # E-full: the locked member now rejects outsiders with busy (was distance)
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
    assert response.json() == {"success": False, "reason": MSG_TARGET_BUSY}

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


# --------------------------------------------------------------------------- #
# E-full: conversation lock + queued actions
# --------------------------------------------------------------------------- #


def test_lock_queues_actions_and_fires_on_leave(world_config: ParsedWorldConfig) -> None:
    """The core E-full lifecycle: starting a conversation locks both members;
    move/wait/sleep/work queue (newest wins) instead of rejecting; leave
    unlocks and expedites the queued action to the next tick."""
    eng = make_engine(world_config)
    runtime = eng.create_world("对话锁")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    started = world_time(world_id)
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True and reason is None
    convo = conversations_for(world_id)[0]

    # lock + hard cap scheduled for both members
    session = SessionLocal()
    try:
        for agent_id in ("agent_linxia", "agent_zhangming"):
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            assert agent.action_type == "talk"
            assert agent.action_ends_at == started + TALK_LOCK_SECONDS
            assert agent.action_data["conversation_id"] == convo.conversation_id
            expired = list(
                session.scalars(
                    select(ScheduledAction).where(
                        ScheduledAction.world_id == world_id,
                        ScheduledAction.agent_id == agent_id,
                        ScheduledAction.action_type == "talk_expired",
                    )
                )
            )
            assert len(expired) == 1
            assert expired[0].due_at == started + TALK_LOCK_SECONDS
    finally:
        session.close()

    # move while locked -> queued (ok, no move started)
    ok, envelope, reason = eng.action_service.execute_move(
        world_id, "agent_linxia", "village_hotel", reason="去旅店"
    )
    assert ok is True and envelope is None
    assert reason == MSG_QUEUED
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert linxia.action_type == "talk"  # still locked in place
        queued = list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == "agent_linxia",
                    ScheduledAction.action_type == "queued_action",
                )
            )
        )
        assert len(queued) == 1
        assert queued[0].payload["tool"] == "move"
        assert queued[0].payload["arguments"]["destination_id"] == "village_hotel"
    finally:
        session.close()

    # a second queued action replaces the first (one queued per agent)
    ok, envelope, reason = eng.action_service.execute_wait(
        world_id, "agent_linxia", minutes=10, reason="歇会儿"
    )
    assert ok is True and envelope is None
    assert reason == MSG_QUEUED
    session = SessionLocal()
    try:
        queued = list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == "agent_linxia",
                    ScheduledAction.action_type == "queued_action",
                )
            )
        )
        assert len(queued) == 1 and queued[0].payload["tool"] == "wait"
    finally:
        session.close()

    # leave -> conversation ends, locks cleared, queued wait expedited
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "我先走了", "leave"
    )
    assert ok is True
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "leave"
    session = SessionLocal()
    try:
        for agent_id in ("agent_linxia", "agent_zhangming"):
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            assert agent.action_type is None, f"{agent_id} still locked"
        queued = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.agent_id == "agent_linxia",
                ScheduledAction.action_type == "queued_action",
            )
        ).one()
        assert queued.due_at == world_time(world_id)
    finally:
        session.close()

    # the next tick executes the queued wait
    advance_minutes(eng, world_id, 1)
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert linxia.action_type == "wait"
        assert linxia.action_data["reason"] == "歇会儿"
    finally:
        session.close()
    eng._runtimes.clear()


def test_talk_lock_decision_reams_schedule(world_config: ParsedWorldConfig) -> None:
    """Regression: a decision completing while the agent is mid-conversation
    (action_type == "talk") must re-arm the decide loop at the reply-grace
    cadence. This previously crashed with UnboundLocalError on `floor`."""
    eng = make_engine(world_config)
    runtime = eng.create_world("对话锁重排")
    world_id = runtime.world_id

    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        linxia.action_type = "talk"
        linxia.action_started_at = world_time(world_id)
        linxia.action_ends_at = world_time(world_id) + TALK_LOCK_SECONDS
        linxia.action_data = {"conversation_id": "conv_demo"}
        session.commit()

        # the exact call that crashed in production (ok=True, agent locked)
        eng.decision_service._schedule_next(
            session, runtime, world_id, "agent_linxia", ok=True
        )
        session.commit()

        rearm = list(
            session.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.world_id == world_id,
                    ScheduledAction.agent_id == "agent_linxia",
                    ScheduledAction.action_type == "agent_decide",
                )
            )
        )
        assert len(rearm) == 1
        assert rearm[0].payload == {"origin": "talk_lock"}
        assert rearm[0].due_at == world_time(world_id) + TALK_REPLY_GRACE
    finally:
        session.close()
    eng._runtimes.clear()


def test_talk_lock_refreshes_on_message(world_config: ParsedWorldConfig) -> None:
    """A delivered message extends the silence window — an active conversation
    never hits the hard cap; only true silence ends it. Regression: the lock
    used to be a fixed 15-game-minute budget, which slow LLM replies (wall
    time) always blew through."""
    eng = make_engine(world_config)
    runtime = eng.create_world("对话刷新")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    started = world_time(world_id)
    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        first_end = linxia.action_ends_at
    finally:
        session.close()
    # tests run at speed=1: 60 wall seconds == 60 game minutes
    assert first_end == started + TALK_LOCK_SECONDS

    # a second message inside the window extends it for both members
    advance_minutes(eng, world_id, 10)
    ok, reason, _ = service.send_message(
        world_id, "agent_zhangming", "agent_linxia", "早呀，今天忙吗", "chat"
    )
    assert ok is True and reason is None
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        zhangming = session.get(
            Agent, {"world_id": world_id, "agent_id": "agent_zhangming"}
        )
        assert linxia.action_ends_at == world_time(world_id) + TALK_LOCK_SECONDS
        assert linxia.action_ends_at > first_end
        assert zhangming.action_ends_at == linxia.action_ends_at
    finally:
        session.close()
    eng._runtimes.clear()


def test_talk_expired_timeout(world_config: ParsedWorldConfig) -> None:
    """A silent conversation is force-ended at TALK_LOCK_SECONDS so neither
    member stays locked forever; both locks are released."""
    eng = make_engine(world_config)
    runtime = eng.create_world("对话超时")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is None

    # nobody talks: the lock cap fires and ends the conversation
    advance_minutes(eng, world_id, TALK_LOCK_SECONDS + 1)
    convo = conversations_for(world_id)[0]
    assert convo.ended_at is not None and convo.end_reason == "timeout"
    ended = [e for e in eng.events_after(world_id, 0) if e.type == "conversation_ended"]
    assert ended and ended[0].payload["reason"] == "timeout"
    session = SessionLocal()
    try:
        for agent_id in ("agent_linxia", "agent_zhangming"):
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            assert agent.action_type is None, f"{agent_id} still locked"
    finally:
        session.close()
    eng._runtimes.clear()


def test_stale_talk_lock_repaired(world_config: ParsedWorldConfig) -> None:
    """A talk lock pointing at a nonexistent/ended conversation (crash, god
    interrupt, manual edit) is lazily repaired instead of wedging the agent."""
    eng = make_engine(world_config)
    runtime = eng.create_world("锁修复")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    service = eng.conversation_service

    ok, reason, _ = service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet"
    )
    assert ok is True

    # corrupt the lock: point linxia at a conversation that does not exist
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        linxia.action_data = {"conversation_id": "conv_ghost"}
        session.commit()
    finally:
        session.close()

    # the stale lock is repaired on the action gate: move proceeds normally
    ok, envelope, reason = eng.action_service.execute_move(
        world_id, "agent_linxia", "village_hotel", reason="去旅店"
    )
    assert ok is True and reason is None and envelope is not None
    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert linxia.action_type == "move"
    finally:
        session.close()
    eng._runtimes.clear()
