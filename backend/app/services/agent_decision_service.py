"""DecisionService: the autonomous loop that turns observations into actions.

One ``decide`` call per agent: build observation -> provider decision ->
record llm_run -> execute the chosen tool through ActionExecutionService ->
schedule the next decision. All DB work is sync SQLite; the provider call is
the only await point and is bounded by llm_timeout_seconds.

The engine's tick loop never awaits this service: the "agent_decide" scheduler
handler spawns an asyncio task (or, under a sync test driver, runs inline).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.context import AgentToolContext
from app.agents.observation_service import build_observation
from app.agents.providers import get_provider
from app.agents.providers.base import DecisionResult
from app.config.settings import Settings, get_settings
from app.database.models.agents import Agent
from app.database.models.llm_runs import LLMRun
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.world_engine.engine import WorldEngine, WorldRuntime

# Delay (game minutes) before the next decision after various outcomes.
RETRY_DELAY = 10  # tool rejected by world rules
IDLE_DELAY = 30  # agent still idle after a decision (periodic re-eval)
DEGRADE_DELAY = 20  # after an LLM failure
DEGRADE_WAIT_MINUTES = 15  # fallback action while the LLM is down
MAX_CONSECUTIVE_FAILURES = 5

CLAMPED_WAIT_MINUTES = (1, 240)


class DecisionService:
    """Owns one world's agents' decision loop."""

    def __init__(
        self,
        engine: WorldEngine,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        provider: Any | None = None,
    ) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._provider = provider if provider is not None else get_provider(self._settings)

    # ------------------------------------------------------------------ #
    # Scheduler handler
    # ------------------------------------------------------------------ #

    def handle_agent_decide(self, session: Session, action: ScheduledAction) -> None:
        """Scheduler callback for the "agent_decide" action type.

        Never blocks the tick: under a running event loop the decision runs as
        a background task; under a sync test driver (no loop) it runs inline.
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        world = session.get(World, action.world_id)
        if world is None or world.paused or not world.autonomous:
            # Paused mid-flight: skip; resume schedules idle agents again.
            return
        trace_id = f"trc_decide_{uuid.uuid4().hex[:12]}"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = self.decide(action.world_id, action.agent_id, trace_id)
        if loop is not None:
            loop.create_task(coro)
        else:  # pragma: no cover - exercised by sync test drivers
            asyncio.run(coro)

    # ------------------------------------------------------------------ #
    # One decision cycle
    # ------------------------------------------------------------------ #

    async def decide(self, world_id: str, agent_id: str, trace_id: str) -> None:
        """Run one full decision cycle; returns early when guards fail."""
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None or world.paused or not world.autonomous:
                return
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None or agent.action_type is not None or agent.is_deciding:
                return
            agent.is_deciding = True
            session.commit()
        finally:
            session.close()

        try:
            await self._run_cycle(world_id, agent_id, trace_id)
        except Exception:  # noqa: BLE001 - a crash must never wedge the flag
            logger.exception(
                "Decision cycle crashed world={} agent={} trace={}",
                world_id,
                agent_id,
                trace_id,
            )
        finally:
            session = self._session_factory()
            try:
                agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
                if agent is not None:
                    agent.is_deciding = False
                session.commit()
            finally:
                session.close()

    async def _run_cycle(self, world_id: str, agent_id: str, trace_id: str) -> None:
        context = AgentToolContext(
            world_id=world_id,
            agent_id=agent_id,
            action_service=self.engine.action_service,
            engine=self.engine,
        )
        observation = build_observation(world_id, agent_id, self._session_factory)

        try:
            result = await asyncio.wait_for(
                self._provider.decide(
                    observation=observation, context=context, trace_id=trace_id
                ),
                timeout=self._settings.llm_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._degrade(world_id, agent_id, trace_id, "timeout", "LLM 决策超时")
            return
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades
            self._degrade(world_id, agent_id, trace_id, type(exc).__name__, str(exc))
            return

        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            runtime = self.engine.get_runtime(world_id)
            if world is None or runtime is None:
                return
            # Record BEFORE executing the tool; success is patched afterwards.
            run = LLMRun(
                world_id=world_id,
                agent_id=agent_id,
                world_time=world.world_time,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=result.latency_ms,
                tool_name=result.tool_name,
                tool_arguments=result.tool_arguments,
                tool_result={},
                success=True,
                error_type=None,
                trace_id=trace_id,
                raw_summary=result.raw_summary[:512],
            )
            session.add(run)
            session.commit()

            ok, envelope, reason = self._execute_tool(result, world_id, agent_id, trace_id)
            run.success = ok
            run.tool_result = {
                "success": ok,
                "reason": reason,
                "event": envelope.model_dump() if envelope is not None else None,
            }
            session.commit()

            paused = bool(
                session.scalar(select(World.paused).where(World.world_id == world_id))
            )
            if not paused:
                self._schedule_next(session, runtime, world_id, agent_id, ok=ok)
                session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Tool execution + scheduling
    # ------------------------------------------------------------------ #

    def _execute_tool(
        self, result: DecisionResult, world_id: str, agent_id: str, trace_id: str
    ) -> tuple[bool, Any, str | None]:
        """Run the chosen tool through the action service (world rule gate)."""
        service = self.engine.action_service
        arguments = result.tool_arguments or {}
        if result.tool_name == "move":
            return service.execute_move(
                world_id,
                agent_id,
                arguments.get("destination_id"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        if result.tool_name == "wait":
            minutes = arguments.get("minutes")
            if minutes is not None:
                minutes = max(CLAMPED_WAIT_MINUTES[0], min(int(minutes), CLAMPED_WAIT_MINUTES[1]))
            return service.execute_wait(
                world_id,
                agent_id,
                minutes=minutes,
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        return False, None, f"未知工具: {result.tool_name}"

    def _schedule_next(
        self,
        session: Session,
        runtime: WorldRuntime,
        world_id: str,
        agent_id: str,
        *,
        ok: bool,
    ) -> None:
        """Queue the next decision.

        - action started -> nothing (the completion handler schedules it)
        - tool rejected  -> +10 game minutes (agent adjusts, T3-9)
        - still idle     -> +30 game minutes (periodic re-eval)
        """
        world_time = int(
            session.scalar(select(World.world_time).where(World.world_id == world_id)) or 0
        )
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        if agent is None:
            return
        if agent.action_type is not None:
            return  # completion handler schedules the next decision
        floor = max(self._settings.decision_min_interval, 1)
        if ok:
            delay = max(IDLE_DELAY, floor)
        else:
            delay = max(RETRY_DELAY, floor)
        runtime.scheduler.schedule(
            session, agent_id, "agent_decide", world_time + delay, {"origin": "retry" if not ok else "idle"}
        )

    # ------------------------------------------------------------------ #
    # LLM failure degradation (T3-10)
    # ------------------------------------------------------------------ #

    def _degrade(
        self, world_id: str, agent_id: str, trace_id: str, error_type: str, detail: str
    ) -> None:
        """Record the failed run, start a fallback wait, keep the world ticking."""
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            runtime = self.engine.get_runtime(world_id)
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if world is None or runtime is None or agent is None:
                return
            logger.warning(
                "LLM decision failed world={} agent={} type={}: {}",
                world_id,
                agent_id,
                error_type,
                detail,
            )
            session.add(
                LLMRun(
                    world_id=world_id,
                    agent_id=agent_id,
                    world_time=world.world_time,
                    model=self._settings.llm_model,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    tool_name="",
                    tool_arguments={},
                    tool_result={},
                    success=False,
                    error_type=error_type,
                    trace_id=trace_id,
                    raw_summary=f"[degrade] {error_type}: {detail}"[:512],
                )
            )
            agent.consecutive_failures = min(
                (agent.consecutive_failures or 0) + 1, MAX_CONSECUTIVE_FAILURES
            )
            session.commit()

            if agent.action_type is None:
                self.engine.action_service.execute_wait(
                    world_id,
                    agent_id,
                    minutes=DEGRADE_WAIT_MINUTES,
                    reason="LLM 故障降级等待",
                    trace_id=trace_id,
                )
            paused = bool(
                session.scalar(select(World.paused).where(World.world_id == world_id))
            )
            if not paused:
                floor = max(self._settings.decision_min_interval, 1)
                runtime.scheduler.schedule(
                    session,
                    agent_id,
                    "agent_decide",
                    world.world_time + max(DEGRADE_DELAY, floor),
                    {"origin": "degrade"},
                )
                session.commit()
        finally:
            session.close()
