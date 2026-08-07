"""M3 tests: LLM decision loop (fake provider), T3-9 adjustment, T3-10 degradation.

Drives the WorldEngine + DecisionService directly (no HTTP, no background
loop): clock advanced via tick + engine._tick_runtime, exactly like
test_world_engine.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.providers.base import DecisionResult
from app.agents.providers.fake_provider import FakeDecisionProvider
from app.config.settings import get_settings
from app.database.models.agents import Agent
from app.database.models.llm_runs import LLMRun
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.database.session import SessionLocal
from app.services.action_execution_service import ActionExecutionService
from app.services.agent_decision_service import DecisionService
from app.services.world_config_loader import ParsedWorldConfig, load_world_config
from app.world_engine.engine import WorldEngine
from tests.test_world_engine import advance_minutes


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
    return eng


def pending_decides(session, world_id: str) -> int:
    return len(
        session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.action_type == "agent_decide",
            )
        ).all()
    )


def test_autonomous_world_agents_follow_scripts(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("自主世界", autonomous=True)
    world_id = runtime.world_id

    session = SessionLocal()
    try:
        # initial decisions scheduled staggered
        assert pending_decides(session, world_id) == 9
    finally:
        session.close()

    # 20 game minutes: every agent should have made at least one decision
    advance_minutes(eng, world_id, 20)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) >= 6, f"expected >=6 decision runs, got {len(runs)}"
        by_agent: dict[str, list[LLMRun]] = {}
        for run in runs:
            by_agent.setdefault(run.agent_id, []).append(run)
        assert set(by_agent) == {
            "agent_linxia", "agent_zhangming", "agent_chenyu",
            "agent_wangfang", "agent_laozhang", "agent_touzi",
            "agent_zhoushen", "agent_limujiang", "agent_sunshen",
        }
        # linxia moved to the shop per script (first decision)
        linxia_runs = sorted(by_agent["agent_linxia"],
                             key=lambda r: r.created_at)
        assert linxia_runs[0].tool_name == "move"
        assert linxia_runs[0].tool_arguments["destination_id"] == "village_shop"
        assert linxia_runs[0].success == 1
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert linxia.action_type is not None  # move started
        # trace ids present and unique
        assert len({r.trace_id for r in runs}) == len(runs)
        assert all(r.trace_id.startswith("trc_") for r in runs)
        world = session.get(World, world_id)
        assert world.autonomous is True
    finally:
        session.close()
    eng._runtimes.clear()


def test_t39_failure_then_adjust(world_config: ParsedWorldConfig) -> None:
    """chenyu's script opens with ghost_town (rejected) then recovers (T3-9)."""
    eng = make_engine(world_config)
    runtime = eng.create_world("自主世界2", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 10)  # first decide: ghost_town -> rejected

    session = SessionLocal()
    try:
        chenyu_runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id, LLMRun.agent_id == "agent_chenyu"
            )
        ).all()
        assert len(chenyu_runs) == 1
        assert chenyu_runs[0].tool_name == "move"
        assert chenyu_runs[0].tool_arguments["destination_id"] == "ghost_town"
        assert chenyu_runs[0].success == 0
        assert chenyu_runs[0].tool_result["reason"]  # rejection reason recorded
    finally:
        session.close()

    # next decision: recovers to village_plaza (observation showed the failure)
    advance_minutes(eng, world_id, 12)
    session = SessionLocal()
    try:
        chenyu_runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id, LLMRun.agent_id == "agent_chenyu"
            )
        ).all()
        assert len(chenyu_runs) >= 2
        # M4 relaxation: assert the SECOND decision (chenyu_runs[1]) is the
        # successful recovery move, not necessarily the last run — once chenyu
        # reaches the plaza, later decisions may be talk (M4 conversations)
        # instead of more moves.
        recovery = chenyu_runs[1]
        assert recovery.tool_name == "move"
        assert recovery.tool_arguments["destination_id"] == "village_plaza"
        assert recovery.success == 1
        chenyu = session.get(Agent, {"world_id": world_id, "agent_id": "agent_chenyu"})
        assert chenyu.action_type == "move"
    finally:
        session.close()
    eng._runtimes.clear()


def test_t310_llm_failure_degrades_to_wait(world_config: ParsedWorldConfig) -> None:
    class BrokenProvider:
        async def decide(self, *, observation: str, context, trace_id: str):
            raise TimeoutError("LLM 无响应")

    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.decision_service = DecisionService(eng, SessionLocal, provider=BrokenProvider())
    runtime = eng.create_world("故障世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 10)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) == 9, "every agent degraded"
        assert all(r.success == 0 for r in runs)
        assert all(r.error_type for r in runs)
        # every agent fell back to a wait action (world kept ticking)
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        for agent in agents:
            assert agent.action_type == "wait", f"{agent.agent_id} not degraded to wait"
            assert agent.consecutive_failures >= 1
        # next decisions still scheduled (recovery path)
        assert pending_decides(session, world_id) >= 9
    finally:
        session.close()
    eng._runtimes.clear()


def test_non_autonomous_world_makes_no_decisions(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("静态世界")  # autonomous=False
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 60)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert runs == []
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        assert all(a.action_type is None for a in agents)
    finally:
        session.close()
    eng._runtimes.clear()


def test_paused_autonomous_world_frozen(world_config: ParsedWorldConfig) -> None:
    eng = make_engine(world_config)
    runtime = eng.create_world("暂停世界", autonomous=True)
    world_id = runtime.world_id
    eng.set_paused(world_id, True)

    # bounded ticks while paused (clock frozen -> advance_minutes would loop)
    runtime = eng.get_runtime(world_id)
    for _ in range(30):
        runtime.clock.tick(0.9)
        eng._tick_runtime(runtime)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert runs == [], "no decisions while paused"
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        assert all(a.action_type is None for a in agents)
    finally:
        session.close()

    eng.set_paused(world_id, False)
    advance_minutes(eng, world_id, 20)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) >= 9, "resume re-armed decisions"
        assert {r.agent_id for r in runs} == {
            "agent_linxia", "agent_zhangming", "agent_chenyu",
            "agent_wangfang", "agent_laozhang", "agent_touzi",
            "agent_zhoushen", "agent_limujiang", "agent_sunshen",
        }
    finally:
        session.close()
    eng._runtimes.clear()


def test_decision_loop_survives_restart(world_config: ParsedWorldConfig) -> None:
    """Autonomous worlds re-arm decisions after an engine restart (load_existing)."""
    eng = make_engine(world_config)
    runtime = eng.create_world("重启世界", autonomous=True)
    world_id = runtime.world_id
    advance_minutes(eng, world_id, 10)

    # simulate restart: new engine, same DB
    eng2 = make_engine(world_config)
    eng2.load_existing()
    runtime2 = eng2.get_runtime(world_id)
    assert runtime2 is not None
    advance_minutes(eng2, world_id, 20)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) >= 6, "decisions resumed after restart"
    finally:
        session.close()
    eng._runtimes.clear()
    eng2._runtimes.clear()


class ToolOutputProvider:
    """Decision provider mimicking the real OpenAI SDK path: the tool was
    already executed and ``tool_output`` carries its JSON result. The service
    must parse it, record the run and re-arm the loop."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *, observation, context, trace_id) -> DecisionResult:
        self.calls += 1
        return DecisionResult(
            tool_name="buy_stock",
            tool_arguments={"stock_id": "stock_village_shop", "shares": 1, "reason": "投资"},
            model="fake",
            input_tokens=0,
            output_tokens=0,
            latency_ms=1,
            raw_summary="[tool] buy_stock",
            tool_output=json.dumps(
                {"success": True, "reason": None, "event": None}, ensure_ascii=False
            ),
        )


def test_tool_output_cycle_does_not_crash_and_rearms(world_config: ParsedWorldConfig) -> None:
    """Regression: a missing ``json`` import crashed every real-LLM decision
    cycle that carried tool_output, so the loop died after the first decision
    per agent (no re-arm, silent town). The parse must survive and schedule
    the next decision."""
    provider = ToolOutputProvider()
    eng = make_engine(world_config)
    eng.decision_service = DecisionService(eng, SessionLocal, provider=provider)
    runtime = eng.create_world("输出循环", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 5)  # first decision
    advance_minutes(eng, world_id, 40)  # re-arm after IDLE_DELAY (30)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) >= 2, "loop must survive the tool_output parse and re-arm"
        assert all(r.success == 1 for r in runs)
        assert all(r.tool_name == "buy_stock" for r in runs)
    finally:
        session.close()
    eng._runtimes.clear()
