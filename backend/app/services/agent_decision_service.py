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
import hashlib
import json
import re
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agents.context import AgentToolContext
from app.agents.observation_service import build_observation
from app.agents.providers import get_provider
from app.agents.providers.base import DecisionResult
from app.config.gameplay import (
    BUDGET_SKIP_DELAY,
    DEGRADE_DELAY,
    DEGRADE_WAIT_MINUTES,
    FORCED_REST_MINUTES,
    IDLE_DELAY,
    MAX_CONSECUTIVE_FAILURES,
    RETRY_DELAY,
    SKIP_DECIDE_DELAY,
    SLEEP_MAX_MINUTES,
    SLEEP_MIN_MINUTES,
    TALK_REPLY_GRACE,
    WAIT_MAX_MINUTES,
    WAIT_MIN_MINUTES,
)
from app.config.settings import Settings, get_settings
from app.database.models.agents import Agent
from app.database.models.llm_runs import LLMRun
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.domain.event import WorldEventEnvelope
from app.services.company_employment_service import CompanyEmploymentError
from app.world_engine.engine import WorldEngine, WorldRuntime

# M8 T8: one text event emitted (once per day) when the world budget runs out.
BUDGET_EXHAUSTED_TEXT = "世界今日 LLM 预算已用尽，智能体转入休眠节奏"

CLAMPED_WAIT_MINUTES = (WAIT_MIN_MINUTES, WAIT_MAX_MINUTES)
CLAMPED_SLEEP_MINUTES = (SLEEP_MIN_MINUTES, SLEEP_MAX_MINUTES)

# --------------------------------------------------------------------------- #
# M8 T8-1: global LLM concurrency cap.
#
# One semaphore shared by every DecisionService instance. asyncio primitives
# are loop-bound, and the sync test driver runs each decision in its own
# ``asyncio.run`` loop — so the semaphore is keyed by the running loop: within
# one loop the cap is global, across loops (sequential test decisions) a fresh
# semaphore with the same limit is used. ``reflect`` shares the same cap.
# --------------------------------------------------------------------------- #
_LLM_SEMAPHORE: asyncio.Semaphore | None = None
_LLM_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _llm_semaphore() -> asyncio.Semaphore:
    """The loop-keyed global semaphore enforcing ``llm_max_concurrent``."""
    global _LLM_SEMAPHORE, _LLM_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _LLM_SEMAPHORE is None or _LLM_SEMAPHORE_LOOP is not loop:
        _LLM_SEMAPHORE = asyncio.Semaphore(get_settings().llm_max_concurrent)
        _LLM_SEMAPHORE_LOOP = loop
    return _LLM_SEMAPHORE


def _is_transient(exc: Exception) -> bool:
    """M8 T8-4: errors worth one automatic retry (timeout / transport)."""
    if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, OSError):
        return True
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return False
    return isinstance(exc, httpx.HTTPError)


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
        # M8 T8-5: per-agent observation cache: (obs_hash, world_time).
        self._observation_cache: dict[tuple[str, str], tuple[str, int]] = {}
        # M8 T8: per (world, day) flag: the budget-exhausted event was emitted.
        self._budget_notified: set[tuple[str, int]] = set()

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

    def handle_queued_action(self, session: Session, action: ScheduledAction) -> None:
        """Execute an action queued while the agent was locked in a conversation.

        Fires at the lock's hard cap, or right after the conversation ends
        (the unlock path reschedules it to now). Re-dispatches through
        ``_execute_tool`` so the world rule gates run once more against the
        agent's current state; a rejection is remembered and the decision
        loop re-armed.
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        payload = action.payload or {}
        tool_name = payload.get("tool")
        if not tool_name:
            return
        result = DecisionResult(
            tool_name=tool_name,
            tool_arguments=payload.get("arguments") or {},
            model="queued",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            raw_summary="queued_action",
        )
        ok, _, reason = self._execute_tool(
            result, action.world_id, action.agent_id, str(payload.get("trace_id") or "")
        )
        agent = session.get(Agent, {"world_id": action.world_id, "agent_id": action.agent_id})
        if agent is None:
            return
        if not ok and reason:
            self.engine.memory_recorder.record_llm_failure(
                session, action.world_id, action.agent_id, reason or "排队行动执行失败"
            )
        if agent.action_type is None:
            # the action did not start (rejected) or finished instantly:
            # re-arm the loop exactly like a normal decision.
            self._schedule_next(session, runtime, action.world_id, action.agent_id, ok=ok)

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
            if agent is None or agent.action_type not in (None, "talk"):
                # E-full: a talk lock still needs decisions (reply cadence);
                # every other action defers to its completion handler.
                return
            # Atomic claim. The ORM read-then-write below used to race: two
            # pending agent_decide rows (decision loop + action-completion
            # re-arm) can fire in the same tick, and under SQLite snapshots
            # both cycles saw is_deciding=False -> two concurrent LLM calls
            # and two tool executions for one agent. A conditional UPDATE
            # cannot double-claim.
            claimed = session.execute(
                update(Agent)
                .where(
                    Agent.world_id == world_id,
                    Agent.agent_id == agent_id,
                    Agent.is_deciding.is_(False),
                )
                .values(is_deciding=True, last_decision_at=world.world_time)
            )
            if claimed.rowcount != 1:
                return
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
        # M5 R12: forced rest — an exhausted agent (energy <= 0) may not move
        # or work, so skip the LLM entirely and start a recovery wait. No
        # llm_run row is recorded; the wait_completed handler re-arms the
        # decision loop at the end of the rest.
        session = self._session_factory()
        try:
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is not None and agent.energy <= 0:
                ok, _, _ = self.engine.action_service.execute_wait(
                    world_id,
                    agent_id,
                    minutes=FORCED_REST_MINUTES,
                    reason="精力耗尽，强制休息",
                    trace_id=trace_id,
                )
                runtime = self.engine.get_runtime(world_id)
                if ok and runtime is not None:
                    runtime.event_bus.publish(
                        session,
                        runtime.clock.world_time,
                        "world_event_created",
                        {
                            "agent_id": agent_id,
                            "text": f"{agent.name} 精力耗尽，正在休息",
                            "importance": "normal",
                        },
                    )
                    session.commit()
                return
        finally:
            session.close()

        context = AgentToolContext(
            world_id=world_id,
            agent_id=agent_id,
            action_service=self.engine.action_service,
            engine=self.engine,
            trace_id=trace_id,
        )
        observation = build_observation(
            world_id,
            agent_id,
            self._session_factory,
            memory_service=self.engine.memory_service,
            home_id=self.engine.home_location_id(agent_id),
            engine=self.engine,
        )

        # M8: observation cache (T8-5) + world daily budget (T8) gates run
        # before any LLM call; a skip schedules its own follow-up decision.
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            runtime = self.engine.get_runtime(world_id)
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if world is None or runtime is None or agent is None:
                return
            if not self._observation_or_budget_gate(
                    session, runtime, world, agent, observation, world.world_time
            ):
                return
        finally:
            session.close()

        result = await self._call_decision(
            observation, context, trace_id, world_id, agent_id
        )
        if result is None:
            return  # retry exhausted: _degrade recorded the failure + backoff

        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            runtime = self.engine.get_runtime(world_id)
            if world is None or runtime is None:
                return
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is not None:
                # M8: daily cost counters; a success resets the failure streak
                # (the degrade backoff formula keys off consecutive_failures).
                agent.daily_call_count = (agent.daily_call_count or 0) + 1
                agent.daily_token_usage = (
                        (agent.daily_token_usage or 0)
                        + result.input_tokens
                        + result.output_tokens
                )
                agent.consecutive_failures = 0
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

            # The real LLM provider's SDK loop already executed the tool and
            # returned its output; re-executing here would double the world
            # action (second call rejected as mid-action / duplicate). Use the
            # output when it parses; otherwise fall back to executing (fake
            # provider / fallback path).
            ok: bool | None = None
            envelope = None
            reason: str | None = None
            if result.tool_output:
                try:
                    parsed = json.loads(result.tool_output)
                    if isinstance(parsed, dict) and "success" in parsed:
                        ok = bool(parsed["success"])
                        reason = parsed.get("reason")
                        event_data = parsed.get("event")
                        if isinstance(event_data, dict):
                            try:
                                envelope = WorldEventEnvelope.model_validate(event_data)
                            except Exception:  # noqa: BLE001 - envelope is audit-only
                                envelope = None
                except (ValueError, TypeError):
                    ok = None
            if ok is None:
                ok, envelope, reason = self._execute_tool(
                    result, world_id, agent_id, trace_id
                )
            run.success = ok
            run.tool_result = {
                "success": ok,
                "reason": reason,
                "event": envelope.model_dump() if envelope is not None else None,
            }
            if not ok:
                # M6 T6-3: a rejected tool is an observed failure the agent
                # remembers (working memory, low importance).
                self.engine.memory_recorder.record_llm_failure(
                    session, world_id, agent_id, reason or "工具执行失败"
                )
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
    # M8 T8: pre-LLM gates + provider call with retry-once
    # ------------------------------------------------------------------ #

    def _observation_or_budget_gate(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            agent: Agent,
            observation: str,
            world_time: int,
    ) -> bool:
        """Run before any LLM call; returns True to proceed, False to skip.

        Two skips schedule their own follow-up decision (and commit):
        - observation cache hit (T8-5): identical observation while idle and
          inside the cache window -> re-evaluate in SKIP_DECIDE_DELAY.
        - world daily token budget exhausted (T8): -> dormant cadence.
        """
        # Hash the observation WITHOUT the clock time: the clock ticks every
        # minute, so a raw hash would never match. Day/weather stay in, so a
        # real world change still breaks the cache.
        normalized = re.sub(r"\d{2}:\d{2}", "", observation)
        obs_hash = hashlib.md5(normalized.encode("utf-8")).hexdigest()
        cached = self._observation_cache.get((world.world_id, agent.agent_id))
        window = self._settings.observation_cache_window_minutes
        if (
                cached is not None
                and cached[0] == obs_hash
                and agent.action_type is None
                and window > 0
                and world_time - cached[1] < window
        ):
            runtime.scheduler.schedule(
                session,
                agent.agent_id,
                "agent_decide",
                world_time + SKIP_DECIDE_DELAY,
                {"origin": "cache"},
            )
            session.commit()
            return False
        self._observation_cache[(world.world_id, agent.agent_id)] = (obs_hash, world_time)

        budget = self._settings.world_daily_token_budget
        if budget > 0:
            day_start = world_time - (world_time % 1440)
            spent = int(
                session.scalar(
                    select(
                        func.coalesce(
                            func.sum(LLMRun.input_tokens + LLMRun.output_tokens), 0
                        )
                    ).where(
                        LLMRun.world_id == world.world_id,
                        LLMRun.world_time >= day_start,
                    )
                )
                or 0
            )
            if spent >= budget:
                self._handle_budget_exhausted(
                    session, runtime, world, agent, world_time
                )
                return False
        return True

    def _handle_budget_exhausted(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            agent: Agent,
            world_time: int,
    ) -> None:
        """World LLM budget spent: skip the call, keep agents on a slow
        cadence, and announce the dormancy once per day (deduped)."""
        day = world_time // 1440
        if (world.world_id, day) not in self._budget_notified:
            self._budget_notified.add((world.world_id, day))
            runtime.event_bus.publish(
                session,
                world_time,
                "world_event_created",
                {
                    "agent_id": agent.agent_id,
                    "text": BUDGET_EXHAUSTED_TEXT,
                    "importance": "normal",
                },
            )
        runtime.scheduler.schedule(
            session,
            agent.agent_id,
            "agent_decide",
            world_time + BUDGET_SKIP_DELAY,
            {"origin": "budget"},
        )
        session.commit()

    async def _call_decision(
            self,
            observation: str,
            context: AgentToolContext,
            trace_id: str,
            world_id: str,
            agent_id: str,
    ) -> DecisionResult | None:
        """Provider decide under the global semaphore, with retry-once.

        Transient errors (timeout / transport) retry once; the retry reuses
        the same observation (cheaper). A final failure degrades and returns
        None. The semaphore bounds concurrent LLM calls process-wide.
        """
        sem = _llm_semaphore()
        await sem.acquire()
        try:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await asyncio.wait_for(
                        self._provider.decide(
                            observation=observation,
                            context=context,
                            trace_id=trace_id,
                        ),
                        timeout=self._settings.llm_timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - any failure degrades
                    if attempt == 1 and _is_transient(exc):
                        logger.warning(
                            "LLM transient failure (retry once) world={} agent={} "
                            "type={}: {}",
                            world_id,
                            agent_id,
                            type(exc).__name__,
                            exc,
                        )
                        continue
                    self._degrade(
                        world_id, agent_id, trace_id, type(exc).__name__, str(exc)
                    )
                    return None
        finally:
            sem.release()

    # ------------------------------------------------------------------ #
    # Tool execution + scheduling
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Daily reflection (M6 T6-6): provider's optional reflect hook
    # ------------------------------------------------------------------ #

    async def run_daily_reflection(
            self, world_id: str, agent_id: str, digest: str
    ) -> str | None:
        """Ask the provider for a day-summary reflection (summary text).

        Uses the provider's optional ``reflect`` method; providers without it
        (or a failed call) return None and the reflection is skipped.
        """
        reflect = getattr(self._provider, "reflect", None)
        if reflect is None:
            return None
        trace_id = f"trc_reflect_{uuid.uuid4().hex[:12]}"
        sem = _llm_semaphore()
        await sem.acquire()
        try:
            try:
                summary = await asyncio.wait_for(
                    reflect(digest=digest, context=None, trace_id=trace_id),
                    timeout=self._settings.llm_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - a failed reflection is not fatal
                logger.exception(
                    "Reflection failed world={} agent={} trace={}",
                    world_id,
                    agent_id,
                    trace_id,
                )
                return None
        finally:
            sem.release()
        summary = (summary or "").strip()
        return summary or None

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
        if result.tool_name == "talk":
            conversation_service = self.engine.conversation_service
            if conversation_service is None:
                return False, None, "对话服务未初始化"
            ok, reason, envelope = conversation_service.send_message(
                world_id,
                agent_id,
                arguments.get("target_agent_id"),
                arguments.get("message"),
                arguments.get("intent"),
                trace_id=trace_id,
            )
            return ok, envelope, reason
        if result.tool_name == "sleep":
            minutes = arguments.get("minutes")
            if minutes is not None:
                minutes = max(
                    CLAMPED_SLEEP_MINUTES[0], min(int(minutes), CLAMPED_SLEEP_MINUTES[1])
                )
            else:
                minutes = CLAMPED_SLEEP_MINUTES[0]
            return service.execute_sleep(
                world_id,
                agent_id,
                minutes=minutes,
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M5 economy tools (work / buy / sell / use) through the rule gate.
        economy_service = self.engine.economy_service
        if economy_service is None:
            return False, None, "经济服务未初始化"
        if result.tool_name == "work":
            return economy_service.work_start(
                world_id,
                agent_id,
                arguments.get("job_id"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        if result.tool_name == "buy_item":
            quantity = arguments.get("quantity")
            if quantity is not None:
                quantity = max(1, min(int(quantity), 99))
            else:
                quantity = 1
            return economy_service.buy(
                world_id,
                agent_id,
                arguments.get("item_id"),
                quantity=quantity,
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        if result.tool_name == "sell_item":
            quantity = arguments.get("quantity")
            if quantity is not None:
                quantity = max(1, min(int(quantity), 99))
            else:
                quantity = 1
            return economy_service.sell(
                world_id,
                agent_id,
                arguments.get("item_id"),
                quantity=quantity,
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        if result.tool_name == "use_item":
            return economy_service.use_item(
                world_id,
                agent_id,
                arguments.get("item_id"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M18 personal-shop tools through the shop rule gate (R39-R43).
        if result.tool_name in ("open_shop", "stock_shop", "adjust_price", "close_shop"):
            shop_service = self.engine.shop_service
            if shop_service is None:
                return False, None, "店铺服务未初始化"
            if result.tool_name == "open_shop":
                return shop_service.open_shop(
                    world_id,
                    agent_id,
                    arguments.get("location"),
                    arguments.get("products"),
                    reason=arguments.get("reason"),
                    trace_id=trace_id,
                )
            if result.tool_name == "stock_shop":
                quantity = arguments.get("quantity")
                quantity = 1 if quantity is None else max(1, min(int(quantity), 99))
                return shop_service.stock_shop(
                    world_id,
                    agent_id,
                    arguments.get("store_id"),
                    arguments.get("item_id"),
                    quantity=quantity,
                    reason=arguments.get("reason"),
                    trace_id=trace_id,
                )
            if result.tool_name == "adjust_price":
                return shop_service.adjust_price(
                    world_id,
                    agent_id,
                    arguments.get("store_id"),
                    arguments.get("item_id"),
                    arguments.get("new_price"),
                    reason=arguments.get("reason"),
                    trace_id=trace_id,
                )
            return shop_service.close_shop(
                world_id,
                agent_id,
                arguments.get("store_id"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M10 stock tools through the stock rule gate.
        if result.tool_name in ("buy_stock", "sell_stock"):
            stock_service = self.engine.stock_service
            if stock_service is None:
                return False, None, "股票服务未初始化"
            shares = arguments.get("shares")
            shares = 1 if shares is None else max(1, min(int(shares), 9999))
            fn = (
                stock_service.buy_stock
                if result.tool_name == "buy_stock"
                else stock_service.sell_stock
            )
            return fn(
                world_id,
                agent_id,
                arguments.get("stock_id"),
                shares=shares,
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M11 agent-to-agent transfer / give.
        transfer_service = self.engine.transfer_service
        if result.tool_name in ("transfer_money", "give_item"):
            if transfer_service is None:
                return False, None, "转账服务未初始化"
            if result.tool_name == "transfer_money":
                amount = arguments.get("amount")
                amount = 1 if amount is None else max(1, min(int(amount), 1_000_000))
                return transfer_service.transfer_money(
                    world_id, agent_id, arguments.get("target_agent_id"),
                    amount=amount, reason=arguments.get("reason"), trace_id=trace_id,
                )
            quantity = arguments.get("quantity")
            quantity = 1 if quantity is None else max(1, min(int(quantity), 99))
            return transfer_service.give_item(
                world_id, agent_id, arguments.get("target_agent_id"), arguments.get("item_id"),
                quantity=quantity, reason=arguments.get("reason"), trace_id=trace_id,
            )
        # M14 build through the construction rule gate (R22).
        if result.tool_name == "build":
            build_service = self.engine.build_service
            if build_service is None:
                return False, None, "建造服务未初始化"
            return build_service.build_start(
                world_id,
                agent_id,
                arguments.get("col"),
                arguments.get("row"),
                arguments.get("blueprint_id"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M15 plant/harvest through the farming rule gate (R23).
        if result.tool_name in ("plant", "harvest"):
            crop_service = self.engine.crop_service
            if crop_service is None:
                return False, None, "种植服务未初始化"
            if result.tool_name == "plant":
                return crop_service.plant(
                    world_id,
                    agent_id,
                    arguments.get("col"),
                    arguments.get("row"),
                    arguments.get("item_id"),
                    reason=arguments.get("reason"),
                    trace_id=trace_id,
                )
            return crop_service.harvest(
                world_id,
                agent_id,
                arguments.get("col"),
                arguments.get("row"),
                reason=arguments.get("reason"),
                trace_id=trace_id,
            )
        # M13 company tools through the company rule gate (R23-R25).
        if result.tool_name in ("apply_job", "withdraw_job_application", "review_job_application"):
            company_service = getattr(self.engine, "company_employment_service", None)
            if company_service is None:
                return False, None, "企业服务未初始化"
            try:
                if result.tool_name == "apply_job":
                    payload = company_service.apply(
                        world_id,
                        str(arguments.get("opening_id") or ""),
                        agent_id,
                        str(arguments.get("reason") or ""),
                    )
                elif result.tool_name == "withdraw_job_application":
                    payload = company_service.withdraw(
                        world_id,
                        str(arguments.get("application_id") or ""),
                        agent_id,
                    )
                else:
                    payload = company_service.review(
                        world_id,
                        str(arguments.get("application_id") or ""),
                        agent_id,
                        str(arguments.get("decision") or ""),
                        str(arguments.get("reason") or ""),
                    )
                return True, payload, None
            except CompanyEmploymentError as exc:
                return False, None, str(exc)
        # M13 shift + leave tools through the company rule gate (R27-R28).
        if result.tool_name in (
                "start_shift", "resign_job", "request_leave", "review_leave_request",
                "terminate_employment", "pause_recruitment", "resume_recruitment",
        ):
            company_service = getattr(self.engine, "company_employment_service", None)
            if company_service is None:
                return False, None, "企业服务未初始化"
            try:
                if result.tool_name == "start_shift":
                    payload = company_service.start_shift(
                        world_id,
                        str(arguments.get("shift_id") or ""),
                        agent_id,
                    )
                elif result.tool_name == "resign_job":
                    payload = company_service.resign(
                        world_id,
                        str(arguments.get("employment_id") or ""),
                        agent_id,
                        str(arguments.get("reason") or ""),
                    )
                elif result.tool_name == "request_leave":
                    payload = company_service.request_leave(
                        world_id,
                        str(arguments.get("shift_id") or ""),
                        agent_id,
                        str(arguments.get("reason") or ""),
                    )
                elif result.tool_name == "review_leave_request":
                    payload = company_service.review_leave_request(
                        world_id,
                        str(arguments.get("request_id") or ""),
                        agent_id,
                        str(arguments.get("decision") or ""),
                        str(arguments.get("reason") or ""),
                    )
                elif result.tool_name == "terminate_employment":
                    payload = company_service.terminate(
                        world_id,
                        str(arguments.get("employment_id") or ""),
                        agent_id,
                        str(arguments.get("reason") or ""),
                    )
                elif result.tool_name == "pause_recruitment":
                    payload = company_service.pause_recruitment(
                        world_id,
                        str(arguments.get("position_id") or ""),
                        agent_id,
                    )
                else:
                    payload = company_service.resume_recruitment(
                        world_id,
                        str(arguments.get("position_id") or ""),
                        agent_id,
                    )
                return True, payload, None
            except CompanyEmploymentError as exc:
                return False, None, str(exc)
        # M16 company procurement / shelf stocking through the company rule gate.
        if result.tool_name in ("purchase_company_goods", "stock_store"):
            company_service = getattr(self.engine, "company_employment_service", None)
            if company_service is None:
                return False, None, "企业服务未初始化"
            quantity = arguments.get("quantity")
            quantity = 1 if quantity is None else max(1, min(int(quantity), 99))
            try:
                if result.tool_name == "purchase_company_goods":
                    payload = company_service.purchase_company_goods(
                        world_id,
                        str(arguments.get("buyer_company_id") or ""),
                        str(arguments.get("seller_company_id") or ""),
                        agent_id,
                        str(arguments.get("item_id") or ""),
                        quantity=quantity,
                        reason=str(arguments.get("reason") or ""),
                        trace_id=trace_id,
                    )
                else:
                    payload = company_service.stock_store(
                        world_id,
                        str(arguments.get("company_id") or ""),
                        str(arguments.get("store_id") or ""),
                        agent_id,
                        str(arguments.get("item_id") or ""),
                        quantity=quantity,
                        reason=str(arguments.get("reason") or ""),
                        trace_id=trace_id,
                    )
                return True, payload, None
            except CompanyEmploymentError as exc:
                return False, None, str(exc)
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
        floor = max(self._settings.decision_min_interval, 1)
        if agent.action_type == "talk":
            # E-full: a conversation lock never completes, so the usual
            # completion-handler re-arm never fires; keep nudging the locked
            # agent (reply cadence) instead of dropping out of the loop.
            delay = max(TALK_REPLY_GRACE, floor)
            runtime.scheduler.schedule(
                session, agent_id, "agent_decide", world_time + delay,
                {"origin": "talk_lock"},
            )
            return
        if agent.action_type is not None:
            return  # completion handler schedules the next decision
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
            # M8: a failed provider call still counts against the daily budget.
            agent.daily_call_count = (agent.daily_call_count or 0) + 1
            # M6 T6-3: the failed decision is an observed event worth a
            # low-importance working memory.
            self.engine.memory_recorder.record_llm_failure(
                session, world_id, agent_id, detail
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
                # M8 T8-4: backoff escalation — n consecutive failures -> next
                # decision in min(backoff_max_delay, DEGRADE_DELAY * 2**(n-1)).
                n = agent.consecutive_failures
                backoff = min(
                    self._settings.backoff_max_delay, DEGRADE_DELAY * (2 ** (n - 1))
                )
                runtime.scheduler.schedule(
                    session,
                    agent_id,
                    "agent_decide",
                    world.world_time + max(backoff, floor),
                    {"origin": "degrade"},
                )
                session.commit()
        finally:
            session.close()
