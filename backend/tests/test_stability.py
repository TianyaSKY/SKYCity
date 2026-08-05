"""M8 tests: stability, cost control, observability.

Covers the global LLM concurrency cap, retry-once + backoff escalation,
daily counters with day-boundary reset, world token budget, observation
cache, god/public-event decision boosts, and trace_id completeness.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sqlalchemy import select

from app.agents.providers.base import DecisionResult
from app.config.settings import Settings, get_settings
from app.database.models.agents import Agent
from app.database.models.llm_runs import LLMRun
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.world_events import WorldEvent
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


class CountingProvider:
    """Provider that counts calls; optional failure script and latency."""

    def __init__(self, fail_first: bool = False, always_fail: bool = False,
                 tokens: int = 0, delay: float = 0.0) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.tokens = tokens
        self.delay = delay
        self.max_concurrent = 0
        self.current_concurrent = 0

    async def decide(self, *, observation: str, context, trace_id: str) -> DecisionResult:
        self.calls += 1
        self.current_concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.current_concurrent)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.always_fail or (self.fail_first and self.calls == 1):
                raise TimeoutError("模拟超时")
            return DecisionResult(
                tool_name="wait",
                tool_arguments={"minutes": 1, "reason": "稳定性测试"},
                model="counting",
                input_tokens=self.tokens,
                output_tokens=0,
                latency_ms=1,
                raw_summary="[counting] wait",
            )
        finally:
            self.current_concurrent -= 1


def make_engine(world_config: ParsedWorldConfig, provider, settings=None) -> WorldEngine:
    eng = WorldEngine(
        session_factory=SessionLocal,
        world_config=world_config,
        world_data_dir=Path(get_settings().world_data_dir).resolve(),
    )
    eng.action_service = ActionExecutionService(eng, SessionLocal)
    eng.decision_service = DecisionService(
        eng, SessionLocal, settings=settings, provider=provider
    )
    from app.services.god_action_service import GodActionService
    eng.god_action_service = GodActionService(eng, SessionLocal)
    return eng


def scheduled_decides(session, world_id: str, agent_id: str) -> list[ScheduledAction]:
    return list(
        session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world_id,
                ScheduledAction.agent_id == agent_id,
                ScheduledAction.action_type == "agent_decide",
            )
        ).all()
    )


def test_llm_semaphore_caps_concurrency(world_config: ParsedWorldConfig) -> None:
    """Four concurrent provider calls never exceed llm_max_concurrent (2)."""
    provider = CountingProvider(delay=0.2)
    settings = Settings(llm_max_concurrent=2)
    eng = make_engine(world_config, provider, settings)
    eng.create_world("并发世界", autonomous=True)

    async def run() -> None:
        svc = eng.decision_service
        semaphore_loop = []
        # drive 4 decisions directly through the provider path
        tasks = [
            svc._call_decision("obs", None, f"trc_{i}", "world_001", f"agent_{i}")
            for i in range(4)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    asyncio.run(run())
    assert provider.calls == 4
    assert provider.max_concurrent <= 2, (
        f"concurrency cap violated: {provider.max_concurrent}"
    )
    eng._runtimes.clear()


def test_transient_failure_retries_once(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider(fail_first=True)
    eng = make_engine(world_config, provider)
    runtime = eng.create_world("重试世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 8)

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert provider.calls >= 2, "first call failed -> retried once"
        # every agent ended up with a successful run (retry recovered)
        successful = [r for r in runs if r.success]
        assert len(successful) >= 1
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        assert all(a.consecutive_failures == 0 for a in agents)
    finally:
        session.close()
    eng._runtimes.clear()


def test_backoff_escalates_after_repeated_failures(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider(always_fail=True)
    eng = make_engine(world_config, provider)
    runtime = eng.create_world("故障世界", autonomous=True)
    world_id = runtime.world_id

    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        agent.consecutive_failures = 1  # pretend one failure happened before
        session.commit()
    finally:
        session.close()

    advance_minutes(eng, world_id, 8)  # first (seeded) failure -> cf=2 -> backoff 40

    session = SessionLocal()
    try:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        assert agent.consecutive_failures == 2, agent.consecutive_failures
        failed_runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id,
                LLMRun.agent_id == "agent_linxia",
                LLMRun.success == 0,
            )
        ).all()
        assert failed_runs, "the seeded failure chain produced a failed run"
        fail_time = max(r.world_time for r in failed_runs)
        next_decides = scheduled_decides(session, world_id, "agent_linxia")
        world_time = session.get(World, world_id).world_time
        due = [d for d in next_decides if d.due_at >= world_time]
        assert due, "a follow-up decision is scheduled"
        delay = min(d.due_at for d in due) - fail_time
        assert delay >= 40, f"backoff did not escalate: {delay} < 40"
        assert delay <= 120
    finally:
        session.close()
    eng._runtimes.clear()


def test_daily_counters_reset_at_day_boundary(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider(tokens=25)
    # cache off so every decision reaches the provider (deterministic counts)
    settings = Settings(observation_cache_window_minutes=0)
    eng = make_engine(world_config, provider, settings)
    runtime = eng.create_world("计数世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 20)

    session = SessionLocal()
    try:
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        assert all((a.daily_call_count or 0) > 5 for a in agents)
        assert all((a.daily_token_usage or 0) >= 25 * 5 for a in agents)
        assert all(a.last_decision_at is not None for a in agents)
    finally:
        session.close()

    # cross into day 2: counters reset by the hourly tick, then only a few
    # new decisions accumulate in the first minutes of the new day
    advance_minutes(eng, world_id, (1440 - 500) + 5)  # just past midnight

    session = SessionLocal()
    try:
        agents = session.scalars(
            select(Agent).where(Agent.world_id == world_id)
        ).all()
        for agent in agents:
            assert (agent.daily_call_count or 0) < 5, (
                f"{agent.agent_id} counters were not reset: "
                f"{agent.daily_call_count} calls"
            )
            assert (agent.daily_token_usage or 0) < 25 * 5
    finally:
        session.close()
    eng._runtimes.clear()


def test_world_token_budget_stops_decisions(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider(tokens=100)
    settings = Settings(world_daily_token_budget=1)  # tiny budget
    eng = make_engine(world_config, provider, settings)
    runtime = eng.create_world("预算世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 10)  # first wave spends the budget

    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        assert len(runs) >= 1
        texts = [
            e.payload.get("text", "")
            for e in session.scalars(
                select(WorldEvent).where(
                    WorldEvent.world_id == world_id,
                    WorldEvent.type == "world_event_created",
                )
            ).all()
        ]
        assert any("预算已用尽" in t for t in texts)
    finally:
        session.close()

    before = provider.calls
    advance_minutes(eng, world_id, 20)  # more minutes, budget still spent
    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(LLMRun.world_id == world_id)
        ).all()
        # decisions still scheduled (dormant cadence) but no new LLM calls
        assert provider.calls <= before + 1  # at most a race with an in-flight call
        assert len(runs) >= 1
    finally:
        session.close()
    eng._runtimes.clear()


def test_observation_cache_skips_identical_observations(
    world_config: ParsedWorldConfig,
) -> None:
    provider = CountingProvider()
    settings = Settings(observation_cache_window_minutes=10)
    eng = make_engine(world_config, provider, settings)
    runtime = eng.create_world("缓存世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 8)  # first wave of decisions

    session = SessionLocal()
    try:
        linxia = session.get(Agent, {"world_id": world_id, "agent_id": "agent_linxia"})
        # park linxia idle with nothing changing around her
        linxia.action_type = None
        linxia.action_data = None
        linxia.action_ends_at = None
        session.commit()
    finally:
        session.close()

    before = provider.calls
    advance_minutes(eng, world_id, 6)  # within window: cache should skip
    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia"
            )
        ).all()
        new_runs = [r for r in runs if r.world_time >= 480 + 8]
        assert not new_runs, "cache hit should skip the LLM call"
    finally:
        session.close()

    advance_minutes(eng, world_id, 20)  # past the window: calls resume
    session = SessionLocal()
    try:
        runs = session.scalars(
            select(LLMRun).where(
                LLMRun.world_id == world_id, LLMRun.agent_id == "agent_linxia"
            )
        ).all()
        assert any(r.world_time >= 480 + 34 for r in runs), "decisions resumed"
    finally:
        session.close()
    eng._runtimes.clear()


def test_god_action_boosts_target_decision(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider()
    eng = make_engine(world_config, provider)
    runtime = eng.create_world("上帝提升世界", autonomous=False)
    world_id = runtime.world_id

    # enable autonomy AFTER the god action so the boost is observable
    eng.set_autonomous(world_id, True)
    session = SessionLocal()
    try:
        world_time = session.get(World, world_id).world_time
        decides = scheduled_decides(session, world_id, "agent_linxia")
        initial = min(d.due_at for d in decides)
        assert initial >= world_time + 2
    finally:
        session.close()

    eng.god_action_service.apply(
        world_id, "grant_money", "agent_linxia", {"amount": 50}, "测试提升"
    )
    session = SessionLocal()
    try:
        world_time = session.get(World, world_id).world_time
        decides = scheduled_decides(session, world_id, "agent_linxia")
        boosted = min(d.due_at for d in decides)
        assert boosted <= world_time + 1, "god boost should schedule at +1"
    finally:
        session.close()
    eng._runtimes.clear()


def test_all_events_carry_trace_id(world_config: ParsedWorldConfig) -> None:
    provider = CountingProvider()
    eng = make_engine(world_config, provider)
    runtime = eng.create_world("溯源世界", autonomous=True)
    world_id = runtime.world_id

    advance_minutes(eng, world_id, 30)

    session = SessionLocal()
    try:
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id
            )
        ).all()
        assert events
        missing = [e.sequence for e in events if not e.trace_id]
        assert not missing, f"events without trace_id: {missing}"
    finally:
        session.close()
    eng._runtimes.clear()
