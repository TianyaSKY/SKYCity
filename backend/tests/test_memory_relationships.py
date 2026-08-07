"""M6 tests: memories (observed-only recording + weighted retrieval), directional
relationships (system-computed deltas), daily reflection (23:30 cadence),
REST endpoints, and restart persistence.

Drives WorldEngine + services directly (no HTTP, no background loop) except
test_rest_endpoints, which uses the TestClient — the same pattern as
test_conversations.py / test_ws_flow.py.
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
from app.database.models.memories import Memory
from app.database.models.relationships import Relationship
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.main import app
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.conversation_service import ConversationService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes

REFLECTION_SUMMARY_ZERO = "今天完成了0次工作，和0位朋友聊天。明天继续努力。"


@pytest.fixture(scope="module")
def world_config() -> ParsedWorldConfig:
    return load_world_config(get_settings())


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


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
    from app.services.economy_service import EconomyService

    eng.economy_service = EconomyService(eng, SessionLocal)
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


def relationships_for(world_id: str, agent_id: str) -> list[Relationship]:
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Relationship).where(
                    Relationship.world_id == world_id,
                    Relationship.source_agent_id == agent_id,
                )
            )
        )
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
# Memory recording (T6-3: only observed info)
# --------------------------------------------------------------------------- #


def test_conversation_records_memories_for_both_parties(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("对话记忆")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")

    ok, reason, envelope = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_mem1"
    )
    assert ok is True and reason is None
    assert envelope is not None and envelope.type == "conversation_message"

    linxia = memories_for(world_id, "agent_linxia")
    zhangming = memories_for(world_id, "agent_zhangming")
    # The sender remembers what it said (by the recipient's Chinese name).
    assert any(
        m.memory_type == "episodic" and "我对 张明 说：你好呀" in m.text for m in linxia
    )
    # The recipient remembers what it heard (by the sender's Chinese name).
    assert any(
        m.memory_type == "episodic" and "林夏 对我说：你好呀" in m.text for m in zhangming
    )
    # Entities carry the raw agent ids for retrieval.
    sent = next(m for m in linxia if "说：你好呀" in m.text)
    assert sorted(sent.entities_json) == ["agent_linxia", "agent_zhangming"]
    assert sent.importance == 0.6

    # memory_created events are part of the persisted event log.
    event_types = [e.type for e in eng.events_after(world_id, 0)]
    assert event_types.count("memory_created") == 2
    eng._runtimes.clear()


def test_work_completed_records_memory(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("工作记忆")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_farm")

    ok, _, reason = eng.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活", trace_id="trc_work"
    )
    assert ok is True, reason
    advance_minutes(eng, world_id, 121)  # 120-minute job + completion tick

    memories = memories_for(world_id, "agent_linxia")
    work = [m for m in memories if m.memory_type == "episodic" and "工作" in m.text]
    assert any("完成了 农场劳作 工作，获得 30 金币" in m.text for m in work)
    assert any(m.entities_json == ["job_farm_field"] for m in work)
    # The wage (30) also clears the money_changed threshold (|amount| >= 30).
    assert any("金钱变化" in m.text for m in memories)
    eng._runtimes.clear()


def test_llm_failure_records_working_memory(world_config: ParsedWorldConfig) -> None:
    """chenyu's scripted ghost_town move is rejected -> working memory."""
    eng = make_engine(world_config)
    runtime = eng.create_world("失败记忆", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 10)

    memories = memories_for(world_id, "agent_chenyu")
    assert any(
        m.memory_type == "working" and "行动失败" in m.text for m in memories
    ), f"expected a working failure memory, got {[m.text for m in memories]}"
    eng._runtimes.clear()


def test_secrets_are_not_recorded(world_config: ParsedWorldConfig) -> None:
    """A conversation between two agents creates NO memory for a third."""
    eng = make_engine(world_config)
    runtime = eng.create_world("秘密世界")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")

    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "悄悄话", "chat", "trc_secret"
    )
    assert ok is True, reason

    assert memories_for(world_id, "agent_wangfang") == []
    assert memories_for(world_id, "agent_laozhang") == []
    assert len(memories_for(world_id, "agent_linxia")) == 1
    assert len(memories_for(world_id, "agent_zhangming")) == 1
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Retrieval scoring (T6-4)
# --------------------------------------------------------------------------- #


def test_retrieval_importance_then_recency_ordering(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("检索排序")
    world_id = runtime.world_id
    service = eng.memory_service

    service.record(world_id, "agent_linxia", "episodic", "旧而平淡的记忆", 0.3,
                   keywords=["工作"])
    advance_minutes(eng, world_id, 100)
    service.record(world_id, "agent_linxia", "episodic", "重要记忆", 0.9,
                   keywords=["工作"])
    advance_minutes(eng, world_id, 120)
    service.record(world_id, "agent_linxia", "episodic", "最新的普通记忆", 0.4,
                   keywords=["工作"])

    top = service.retrieve(
        world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
        limit=3, session=SessionLocal(), world_time=700,
    )
    assert [m.text for m in top] == ["重要记忆", "最新的普通记忆", "旧而平淡的记忆"]
    eng._runtimes.clear()


def test_retrieval_recency_breaks_importance_tie(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("检索新近")
    world_id = runtime.world_id
    service = eng.memory_service

    service.record(world_id, "agent_linxia", "episodic", "上午的记忆", 0.6,
                   keywords=["工作"])
    advance_minutes(eng, world_id, 200)
    service.record(world_id, "agent_linxia", "episodic", "晚上的记忆", 0.6,
                   keywords=["工作"])

    top = service.retrieve(
        world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
        limit=2, session=SessionLocal(), world_time=700,
    )
    assert [m.text for m in top] == ["晚上的记忆", "上午的记忆"]
    eng._runtimes.clear()


def test_retrieval_bumps_recall_count(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("回忆计数")
    world_id = runtime.world_id
    service = eng.memory_service

    service.record(world_id, "agent_linxia", "episodic", "反复回忆的事", 0.7,
                   keywords=["工作"])
    session = SessionLocal()
    try:
        first = service.retrieve(
            world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
            limit=4, session=session,
        )
        assert len(first) == 1 and first[0].recall_count == 1
        assert first[0].last_recalled_at == 480
        second = service.retrieve(
            world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
            limit=4, session=session,
        )
        assert second[0].recall_count == 2
    finally:
        session.close()
    eng._runtimes.clear()


def test_retrieval_resolved_flag_ranks_lower(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("已解决记忆")
    world_id = runtime.world_id
    service = eng.memory_service

    service.record(world_id, "agent_linxia", "episodic", "未解决的事", 0.6,
                   keywords=["工作"])
    service.record(world_id, "agent_linxia", "episodic", "已解决的事", 0.6,
                   keywords=["工作"])
    session = SessionLocal()
    try:
        resolved = next(
            m for m in service.retrieve(
                world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
                limit=4, session=session,
            )
            if m.text == "已解决的事"
        )
        resolved.resolved = True
        session.commit()

        # The unresolved memory wins the top slot (exclusion under limit=1).
        top1 = service.retrieve(
            world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
            limit=1, session=session,
        )
        assert [m.text for m in top1] == ["未解决的事"]
        top2 = service.retrieve(
            world_id, "agent_linxia", context_entities=[], context_keywords=["工作"],
            limit=2, session=session,
        )
        assert [m.text for m in top2] == ["未解决的事", "已解决的事"]
    finally:
        session.close()
    eng._runtimes.clear()


def test_observation_memory_section_contract(world_config: ParsedWorldConfig) -> None:
    """【相关记忆】 shows retrieved memories or （暂无）; the fake provider's
    parsing sections are untouched."""
    eng = make_engine(world_config)
    runtime = eng.create_world("观察记忆")
    world_id = runtime.world_id

    empty = build_observation(
        world_id, "agent_linxia", SessionLocal, memory_service=eng.memory_service
    )
    assert "【相关记忆】" in empty
    assert "（暂无）" in empty

    eng.memory_service.record(
        world_id, "agent_linxia", "episodic", "我在广场见过张明", 0.6,
        entities=["agent_zhangming"], keywords=["广场"],
    )
    filled = build_observation(
        world_id, "agent_linxia", SessionLocal, memory_service=eng.memory_service
    )
    assert "- [episodic] 我在广场见过张明（重要度 0.6）" in filled
    # The failure marker section the fake provider parses stays intact.
    assert "【上次工具结果】" in filled
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Relationship deltas (T6-5: system-computed, never from the LLM)
# --------------------------------------------------------------------------- #


def test_talk_relationship_deltas_both_directions(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("关系对话")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")

    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_rel1"
    )
    assert ok is True, reason

    session = SessionLocal()
    try:
        linxia_to_zhang = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": "agent_linxia",
             "target_agent_id": "agent_zhangming"},
        )
        assert linxia_to_zhang is not None
        assert (linxia_to_zhang.familiarity, linxia_to_zhang.affection) == (2, 1)
        assert linxia_to_zhang.updated_at == world_time(world_id)
        zhang_to_linxia = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": "agent_zhangming",
             "target_agent_id": "agent_linxia"},
        )
        assert zhang_to_linxia is not None
        assert (zhang_to_linxia.familiarity, zhang_to_linxia.affection) == (1, 0)
    finally:
        session.close()

    # Every non-zero delta emits a relationship_changed event with deltas+values.
    changed = [e for e in eng.events_after(world_id, 0) if e.type == "relationship_changed"]
    assert len(changed) == 2
    by_source = {e.payload["source_agent_id"]: e.payload for e in changed}
    assert by_source["agent_linxia"]["deltas"] == {"familiarity": 2, "affection": 1}
    assert by_source["agent_linxia"]["values"]["familiarity"] == 2
    assert by_source["agent_zhangming"]["deltas"] == {"familiarity": 1}

    # A second chat message accumulates on the speaker->listener row.
    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "在吗", "chat", "trc_rel2"
    )
    assert ok is True, reason
    session = SessionLocal()
    try:
        row = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": "agent_linxia",
             "target_agent_id": "agent_zhangming"},
        )
        assert (row.familiarity, row.affection) == (4, 2)
    finally:
        session.close()
    eng._runtimes.clear()


def test_unrelated_events_leave_relationships_untouched(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("无关事件")
    world_id = runtime.world_id

    ok, _, reason = eng.action_service.execute_wait(
        world_id, "agent_linxia", minutes=10, reason="休息"
    )
    assert ok is True, reason
    ok, _, reason = eng.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is False  # busy, but rejection events produce no relationships
    advance_minutes(eng, world_id, 20)

    session = SessionLocal()
    try:
        rows = session.scalars(
            select(Relationship).where(Relationship.world_id == world_id)
        ).all()
        assert rows == []
    finally:
        session.close()
    eng._runtimes.clear()


def test_relationship_axes_clamp(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("钳制")
    world_id = runtime.world_id
    service = eng.relationship_service

    session = SessionLocal()
    try:
        service.apply_deltas(
            session, world_id, "agent_linxia", "agent_zhangming",
            {"familiarity": 500, "affection": -50, "debt": 5000},
            world_time=480,
        )
        row = service.ensure_row(session, world_id, "agent_linxia", "agent_zhangming")
        assert row.familiarity == 100
        assert row.affection == 0
        assert row.debt == 1000
        session.rollback()
    finally:
        session.close()
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# Daily reflection (T6-6)
# --------------------------------------------------------------------------- #


def test_daily_reflection_fires_once_per_day(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("每日反思")
    world_id = runtime.world_id
    assert world_time(world_id) == 480

    # 08:00 -> 23:30 of day 1 (480 + 930 = 1410).
    advance_minutes(eng, world_id, 930)
    assert world_time(world_id) == 1410

    for agent_id in (
            "agent_linxia", "agent_zhangming", "agent_chenyu",
            "agent_wangfang", "agent_laozhang",
            "agent_touzi", "agent_zhoushen", "agent_limujiang", "agent_sunshen",
    ):
        memories = memories_for(world_id, agent_id)
        semantic = [m for m in memories if m.memory_type == "semantic"]
        assert len(semantic) == 1, f"{agent_id} day-1 reflection missing"
        assert semantic[0].importance == 0.8
        assert semantic[0].keywords_json == ["今日总结"]
        assert semantic[0].text == REFLECTION_SUMMARY_ZERO

    events = [e for e in eng.events_after(world_id, 0) if e.type == "daily_reflection"]
    assert len(events) == 9, "one reflection per agent on day 1"
    assert {e.payload["agent_id"] for e in events} == {
        "agent_linxia", "agent_zhangming", "agent_chenyu",
        "agent_wangfang", "agent_laozhang",
        "agent_touzi", "agent_zhoushen", "agent_limujiang", "agent_sunshen",
    }
    assert all(e.payload["summary"] == REFLECTION_SUMMARY_ZERO for e in events)

    # Day 2: the re-armed action fires exactly once more.
    advance_minutes(eng, world_id, 1440)
    assert world_time(world_id) == 2850
    events = [e for e in eng.events_after(world_id, 0) if e.type == "daily_reflection"]
    assert len(events) == 18  # 9 agents x 2 days
    for agent_id in (
            "agent_linxia", "agent_zhangming", "agent_chenyu",
            "agent_wangfang", "agent_laozhang",
            "agent_touzi", "agent_zhoushen", "agent_limujiang", "agent_sunshen",
    ):
        # Only semantic memories: the day-2 midnight crossing also records an
        # episodic upkeep money_changed memory (M12 upkeep -120 >= threshold).
        assert len(
            [m for m in memories_for(world_id, agent_id) if m.memory_type == "semantic"]
        ) == 2
    eng._runtimes.clear()


def test_daily_reflection_digest_reflects_day_activity(world_config: ParsedWorldConfig) -> None:
    """A day with a completed job produces work-aware reflection summaries."""
    eng = make_engine(world_config)
    runtime = eng.create_world("反思活动")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_farm")
    ok, _, reason = eng.economy_service.work_start(
        world_id, "agent_linxia", "job_farm_field", reason="干活"
    )
    assert ok is True, reason
    advance_minutes(eng, world_id, 130)  # finish the 120-min job

    # 480 + 130 = 610; fast-forward to 23:30 (610 -> 1410).
    advance_minutes(eng, world_id, 800)

    linxia = memories_for(world_id, "agent_linxia")
    summaries = [m.text for m in linxia if m.memory_type == "semantic"]
    assert summaries == ["今天完成了1次工作，和0位朋友聊天。明天继续努力。"]
    eng._runtimes.clear()


# --------------------------------------------------------------------------- #
# REST endpoints
# --------------------------------------------------------------------------- #


def test_rest_memories_and_relationships(client: TestClient) -> None:
    engine = app.state.engine
    response = client.post("/api/worlds", json={"name": "API 记忆"})
    assert response.status_code == 201
    world_id = response.json()["world_id"]

    # Empty shapes before any interaction.
    assert client.get(
        f"/api/worlds/{world_id}/agents/agent_linxia/memories"
    ).json() == []
    assert client.get(
        f"/api/worlds/{world_id}/agents/agent_linxia/relationships"
    ).json() == []

    # Drive a conversation through the engine, then check the contracts.
    park_at(engine, world_id, "agent_linxia", "village_plaza")
    park_at(engine, world_id, "agent_zhangming", "village_plaza")
    ok, reason, _ = engine.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_rest"
    )
    assert ok is True, reason

    memories = client.get(
        f"/api/worlds/{world_id}/agents/agent_linxia/memories?limit=30"
    ).json()
    assert isinstance(memories, list) and len(memories) == 1
    assert set(memories[0]) == {
        "memory_id", "memory_type", "text", "importance", "created_at", "recall_count",
    }
    assert memories[0]["memory_type"] == "episodic"
    assert "你好呀" in memories[0]["text"]
    assert memories[0]["created_at"] == world_time(world_id)

    relationships = client.get(
        f"/api/worlds/{world_id}/agents/agent_linxia/relationships"
    ).json()
    assert isinstance(relationships, list) and len(relationships) == 1
    assert set(relationships[0]) == {
        "source_agent_id", "target_agent_id", "target_name", "familiarity",
        "trust", "affection", "resentment", "debt", "updated_at",
    }
    row = relationships[0]
    assert row["source_agent_id"] == "agent_linxia"
    assert row["target_agent_id"] == "agent_zhangming"
    assert row["target_name"] == "张明"
    assert (row["familiarity"], row["affection"]) == (2, 1)

    # Unknown world -> 404.
    assert client.get(
        "/api/worlds/does_not_exist/agents/agent_linxia/memories"
    ).status_code == 404
    assert client.get(
        "/api/worlds/does_not_exist/agents/agent_linxia/relationships"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Persistence across engine restarts
# --------------------------------------------------------------------------- #


def test_memories_and_relationships_survive_restart(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("重启记忆")
    world_id = runtime.world_id
    park_at(eng, world_id, "agent_linxia", "village_plaza")
    park_at(eng, world_id, "agent_zhangming", "village_plaza")
    ok, reason, _ = eng.conversation_service.send_message(
        world_id, "agent_linxia", "agent_zhangming", "你好呀", "greet", "trc_restart"
    )
    assert ok is True, reason
    before = eng.memory_service.list_memories(world_id, "agent_linxia")
    before_rels = eng.relationship_service.list_for_agent(world_id, "agent_linxia")
    assert before and before_rels

    # Restart: a brand-new engine against the same DB.
    eng2 = make_engine(world_config)
    eng2.load_existing()
    assert eng2.get_runtime(world_id) is not None

    after = eng2.memory_service.list_memories(world_id, "agent_linxia")
    after_rels = eng2.relationship_service.list_for_agent(world_id, "agent_linxia")
    assert after == before
    assert after_rels == before_rels

    # The once-per-day reflection arm is restored without duplicating.
    session = SessionLocal()
    try:
        reflections = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.action_type == "daily_reflection",
            )
        ).all()
        assert len(reflections) == 1
    finally:
        session.close()
    eng._runtimes.clear()
    eng2._runtimes.clear()


# --------------------------------------------------------------------------- #
# Employment lifecycle memories (M13/M16)
# --------------------------------------------------------------------------- #


def _employment_service(eng: WorldEngine) -> "CompanyEmploymentService":
    """Attach a seeded CompanyEmploymentService to an engine (M13)."""
    from app.services.company_employment_service import CompanyEmploymentService

    service = CompanyEmploymentService(
        eng,
        SessionLocal,
        Path(get_settings().world_data_dir).resolve(),
    )
    eng.company_employment_service = service
    return service


def test_apply_reject_hire_shift_resign_records_memories(
        world_config: ParsedWorldConfig,
) -> None:
    """Applying, being rejected, hired, working a shift and resigning all land
    in the applicant's episodic memories (observed-only, applicant side)."""
    eng = make_engine(world_config)
    service = _employment_service(eng)
    runtime = eng.create_world("雇佣记忆")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id

    openings = service.list_openings(world_id)
    farm_opening = next(row for row in openings if row["position_id"] == "position_farm_worker")

    # 1. Submitted, then rejected.
    application = service.apply(world_id, farm_opening["opening_id"], "agent_linxia", "希望获得稳定收入")
    service.review(world_id, application["application_id"], "agent_zhangming", "reject", "名额已满")
    linxia = memories_for(world_id, "agent_linxia")
    texts = [m.text for m in linxia]
    assert any("应聘" in t and "职位" in t for t in texts)
    assert any("被拒绝" in t for t in texts)

    # 2. Re-apply (rejection freed the slot) and get hired.
    application2 = service.apply(world_id, farm_opening["opening_id"], "agent_linxia", "再试一次")
    reviewed = service.review(world_id, application2["application_id"], "agent_zhangming", "accept", "同意录用")
    assert reviewed["employment_id"]
    texts = [m.text for m in memories_for(world_id, "agent_linxia")]
    assert any("录用" in t for t in texts)

    # 3. Work one full shift (company pays -> wage_paid memory).
    employment_view = service.list_agent_employment(world_id, "agent_linxia")
    shift = employment_view["shifts"][0]
    advance_minutes(eng, world_id, shift["scheduled_start"] - runtime.clock.world_time)
    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent is not None
        agent.location_id = "village_farm"
        session.commit()
    finally:
        session.close()
    started = service.start_shift(world_id, shift["shift_id"], "agent_linxia")
    advance_minutes(eng, world_id, started["scheduled_end"] - runtime.clock.world_time)
    texts = [m.text for m in memories_for(world_id, "agent_linxia")]
    assert any("完成了一次班次" in t for t in texts)

    # 4. Resign.
    service.resign(world_id, reviewed["employment_id"], "agent_linxia", "想去别处发展")
    texts = [m.text for m in memories_for(world_id, "agent_linxia")]
    assert any("辞去" in t for t in texts)

    eng._runtimes.clear()


def test_withdrawn_application_records_memory(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    service = _employment_service(eng)
    runtime = eng.create_world("撤回记忆")
    service.register_runtime(runtime)
    service.ensure_seeded(runtime.world_id)
    world_id = runtime.world_id

    openings = service.list_openings(world_id)
    farm_opening = next(row for row in openings if row["position_id"] == "position_farm_worker")
    application = service.apply(world_id, farm_opening["opening_id"], "agent_chenyu", "试试")
    service.withdraw(world_id, application["application_id"], "agent_chenyu")

    texts = [m.text for m in memories_for(world_id, "agent_chenyu")]
    assert any("撤回" in t and "应聘" in t for t in texts)
    assert all("拒绝" not in t for t in texts)

    eng._runtimes.clear()
