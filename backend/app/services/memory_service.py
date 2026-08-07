"""MemoryService: working / episodic / semantic memories with weighted retrieval (M6).

T6-3 (observed-only recording): the MemoryRecorder hooks every published
envelope and writes a memory only for agents that were party to the event —
conversation messages both directions, conversation ends, work completed,
item transactions, large money changes, world events targeting the agent,
god actions (M7), and LLM tool failures (llm_run success=0). Other agents'
secrets are never recorded.

T6-4 (weighted retrieval): score = 0.35*entity_hit + 0.25*keyword_hit
+ 0.2*importance + 0.15*recency (normalised by world_time) + 0.05*(0 if the
memory is resolved else 1). Retrieval bumps last_recalled_at / recall_count.

T6-6 (daily reflection): a per-world recurring scheduled action at 23:30 game
time (minute 1410) builds a day digest per agent, asks the provider's optional
``reflect`` hook for a summary, stores it as a semantic memory
(importance 0.8, keyword 今日总结) and emits daily_reflection. The action
re-arms itself for the next day.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.conversations import Conversation
from app.database.models.memories import Memory
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.world_events import WorldEvent
from app.database.models.worlds import World
from app.domain.event import WorldEventEnvelope

if TYPE_CHECKING:  # pragma: no cover - type hints only (engine imports us)
    from app.world_engine.engine import WorldEngine, WorldRuntime

# Working memory cap: only the newest N working memories per agent survive.
WORKING_MEMORY_CAP = 20

# Daily reflection cadence (T6-6): 23:30 game time = minute 1410 of the day.
REFLECTION_MINUTE_OF_DAY = 23 * 60 + 30  # 1410
# Sentinel agent_id on the per-world recurring reflection action.
REFLECTION_AGENT_ID = "__daily_reflection__"

# M7: memory text templates for god_action_applied, keyed by command type.
# The result dict of the god action is formatted into the template.
_GOD_MEMORY_TEXTS: dict[str, str] = {
    "grant_money": "获得 {amount} 金币",
    "deduct_money": "被扣除 {actual} 金币",
    "spawn_item": "获得 {item_name}×{quantity}",
    "teleport": "被传送到 {location_id}",
    "change_weather": "天气变为 {weather}",
}

# Retrieval weights (T6-4).
WEIGHT_ENTITY = 0.35
WEIGHT_KEYWORD = 0.25
WEIGHT_IMPORTANCE = 0.20
WEIGHT_RECENCY = 0.15
WEIGHT_UNRESOLVED = 0.05

MAX_RETRIEVE_LIMIT = 20


class MemoryService:
    """Owns the memory table: recording, weighted retrieval, daily reflection."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Record
    # ------------------------------------------------------------------ #

    def record(
            self,
            world_id: str,
            agent_id: str,
            memory_type: str,
            text: str,
            importance: float,
            entities: list[str] | None = None,
            keywords: list[str] | None = None,
            resolve_entity: Callable[[str], str | None] | None = None,
            session: Session | None = None,
    ) -> Memory:
        """Persist one memory and emit a memory_created event.

        ``session`` joins the caller's open transaction (the MemoryRecorder
        and the daily reflection pass their session so the memory lands in the
        same commit as the source event); when omitted a private session is
        opened, committed and closed.
        """
        if session is not None:
            return self._record(
                session, world_id, agent_id, memory_type, text, importance,
                entities, keywords, resolve_entity,
            )
        own = self._session_factory()
        try:
            memory = self._record(
                own, world_id, agent_id, memory_type, text, importance,
                entities, keywords, resolve_entity,
            )
            own.commit()
            return memory
        finally:
            own.close()

    def _record(
            self,
            session: Session,
            world_id: str,
            agent_id: str,
            memory_type: str,
            text: str,
            importance: float,
            entities: list[str] | None,
            keywords: list[str] | None,
            resolve_entity: Callable[[str], str | None] | None,
    ) -> Memory:
        world_time = self._world_time(session, world_id)
        entity_ids = list(entities or [])
        if resolve_entity is not None:
            entity_ids = [resolve_entity(entity) or entity for entity in entity_ids]
        memory = Memory(
            memory_id=f"mem_{uuid.uuid4().hex[:16]}",
            world_id=world_id,
            agent_id=agent_id,
            memory_type=memory_type,
            text=(text or "")[:512],
            importance=float(importance),
            entities_json=entity_ids,
            keywords_json=list(keywords or []),
            created_at=world_time,
            last_recalled_at=None,
            recall_count=0,
            resolved=False,
        )
        session.add(memory)
        if memory_type == "working":
            self._prune_working(session, world_id, agent_id)
        self._emit(
            session,
            world_id,
            "memory_created",
            {
                "agent_id": agent_id,
                "memory_id": memory.memory_id,
                "memory_type": memory_type,
                "text": memory.text,
                "importance": memory.importance,
            },
        )
        return memory

    def _prune_working(self, session: Session, world_id: str, agent_id: str) -> None:
        """Keep only the newest ``WORKING_MEMORY_CAP`` working memories."""
        rows = session.scalars(
            select(Memory)
            .where(
                Memory.world_id == world_id,
                Memory.agent_id == agent_id,
                Memory.memory_type == "working",
            )
            .order_by(Memory.created_at.desc(), Memory.memory_id.desc())
        ).all()
        for memory in rows[WORKING_MEMORY_CAP:]:
            session.delete(memory)

    # ------------------------------------------------------------------ #
    # Retrieval (T6-4)
    # ------------------------------------------------------------------ #

    def retrieve(
            self,
            world_id: str,
            agent_id: str,
            context_entities: list[str],
            context_keywords: list[str],
            limit: int = 4,
            session: Session | None = None,
            world_time: int | None = None,
    ) -> list[Memory]:
        """Top ``limit`` memories by weighted score; bumps recall stats.

        ``session`` joins the caller's transaction (the observation builder
        passes its session so the recall bump commits with the read-message
        marks); when omitted a private session is used and committed.
        """
        limit = max(1, min(limit, MAX_RETRIEVE_LIMIT))
        if session is not None:
            return self._retrieve(
                session, world_id, agent_id, context_entities, context_keywords,
                limit, world_time,
            )
        own = self._session_factory()
        try:
            result = self._retrieve(
                own, world_id, agent_id, context_entities, context_keywords,
                limit, world_time,
            )
            own.commit()
            return result
        finally:
            own.close()

    def _retrieve(
            self,
            session: Session,
            world_id: str,
            agent_id: str,
            context_entities: list[str],
            context_keywords: list[str],
            limit: int,
            world_time: int | None,
    ) -> list[Memory]:
        if world_time is None:
            world_time = self._world_time(session, world_id)
        rows = session.scalars(
            select(Memory).where(
                Memory.world_id == world_id, Memory.agent_id == agent_id
            )
        ).all()
        scored = [
            (self._score(memory, context_entities, context_keywords, world_time), memory)
            for memory in rows
        ]
        scored.sort(key=lambda item: (-item[0], -item[1].created_at, item[1].memory_id))
        top = [memory for _, memory in scored[:limit]]
        for memory in top:
            memory.last_recalled_at = world_time
            memory.recall_count += 1
        return top

    @staticmethod
    def _score(
            memory: Memory,
            context_entities: list[str],
            context_keywords: list[str],
            world_time: int,
    ) -> float:
        """Weighted relevance score (T6-4); all terms in [0, 1]."""
        memory_entities = set(memory.entities_json or [])
        if context_entities:
            entity_hit = len(memory_entities & set(context_entities)) / len(context_entities)
        else:
            entity_hit = 0.0
        memory_keywords = set(memory.keywords_json or [])
        if context_keywords:
            keyword_hit = len(memory_keywords & set(context_keywords)) / len(context_keywords)
        else:
            keyword_hit = 0.0
        importance = min(max(memory.importance, 0.0), 1.0)
        recency = memory.created_at / max(world_time, 1)
        unresolved = 0.0 if memory.resolved else 1.0
        return (
                WEIGHT_ENTITY * entity_hit
                + WEIGHT_KEYWORD * keyword_hit
                + WEIGHT_IMPORTANCE * importance
                + WEIGHT_RECENCY * recency
                + WEIGHT_UNRESOLVED * unresolved
        )

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #

    def list_memories(self, world_id: str, agent_id: str, limit: int = 30) -> list[dict]:
        """REST shape for GET .../memories, newest first."""
        session = self._session_factory()
        try:
            rows = session.scalars(
                select(Memory)
                .where(Memory.world_id == world_id, Memory.agent_id == agent_id)
                .order_by(Memory.created_at.desc(), Memory.memory_id.desc())
                .limit(min(max(limit, 1), 500))
            ).all()
            return [
                {
                    "memory_id": memory.memory_id,
                    "memory_type": memory.memory_type,
                    "text": memory.text,
                    "importance": memory.importance,
                    "created_at": memory.created_at,
                    "recall_count": memory.recall_count,
                }
                for memory in rows
            ]
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Daily reflection (T6-6)
    # ------------------------------------------------------------------ #

    def ensure_daily_reflection_scheduled(
            self, session: Session, runtime: WorldRuntime, world_time: int
    ) -> None:
        """Arm the per-world 23:30 reflection unless one is already queued."""
        pending = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.world_id == runtime.world_id,
                ScheduledAction.action_type == "daily_reflection",
            )
        )
        if pending is None:
            runtime.scheduler.schedule(
                session,
                REFLECTION_AGENT_ID,
                "daily_reflection",
                self._next_reflection_time(world_time),
                {},
            )

    @staticmethod
    def _next_reflection_time(world_time: int) -> int:
        """The next 23:30 boundary strictly after ``world_time``."""
        today_start = world_time - (world_time % 1440)
        candidate = today_start + REFLECTION_MINUTE_OF_DAY
        if candidate <= world_time:
            candidate += 1440
        return candidate

    def handle_daily_reflection(
            self, session: Session, action: ScheduledAction
    ) -> None:
        """Scheduler callback: reflect for every agent, re-arm tomorrow.

        The next-day action is scheduled synchronously BEFORE the async
        provider work so a reflection failure never cancels the cadence (the
        once-per-day rate limit lives in the re-arm, not in the provider).
        """
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        world = session.get(World, action.world_id)
        world_time = world.world_time if world is not None else runtime.clock.world_time
        runtime.scheduler.schedule(
            session,
            REFLECTION_AGENT_ID,
            "daily_reflection",
            self._next_reflection_time(world_time),
            {},
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coro = self._run_daily_reflection(action.world_id)
        if loop is not None:
            loop.create_task(coro)
        else:  # pragma: no cover - exercised by sync test drivers
            asyncio.run(coro)

    async def _run_daily_reflection(self, world_id: str) -> None:
        session = self._session_factory()
        try:
            world = session.get(World, world_id)
            if world is None or world.paused:
                return
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return
            world_time = world.world_time
            day_start = world_time - (world_time % 1440)
            agents = session.scalars(
                select(Agent).where(Agent.world_id == world_id).order_by(Agent.agent_id)
            ).all()
            for agent in agents:
                digest = self._build_digest(
                    session, world_id, agent.agent_id, day_start, world_time
                )
                summary = await self._reflect(world_id, agent.agent_id, digest)
                if not summary:
                    continue
                self._record(
                    session, world_id, agent.agent_id, "semantic", summary, 0.8,
                    entities=[agent.agent_id], keywords=["今日总结"], resolve_entity=None,
                )
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "daily_reflection",
                    {"agent_id": agent.agent_id, "summary": summary},
                )
            session.commit()
        finally:
            session.close()

    def _build_digest(
            self, session: Session, world_id: str, agent_id: str, day_start: int, world_time: int
    ) -> str:
        """Short day summary from events the agent was part of (T6-6)."""
        events = session.scalars(
            select(WorldEvent).where(
                WorldEvent.world_id == world_id,
                WorldEvent.world_time >= day_start,
                WorldEvent.world_time <= world_time,
            )
        ).all()
        conversation_ids: set[str] = set()
        friends: set[str] = set()
        purchases = 0
        work = 0
        world_events = 0
        for event in events:
            payload = event.payload or {}
            if event.type == "conversation_message":
                if payload.get("from_agent_id") == agent_id or payload.get("to_agent_id") == agent_id:
                    conversation_ids.add(payload.get("conversation_id") or "")
                    partner = (
                        payload.get("to_agent_id")
                        if payload.get("from_agent_id") == agent_id
                        else payload.get("from_agent_id")
                    )
                    if partner:
                        friends.add(partner)
            elif event.type == "item_purchased" and payload.get("agent_id") == agent_id:
                purchases += 1
            elif event.type == "work_completed" and payload.get("agent_id") == agent_id:
                work += 1
            elif event.type == "world_event_created" and payload.get("agent_id") == agent_id:
                world_events += 1
        day = day_start // 1440 + 1
        return (
            f"第 {day} 天总结（{agent_id}）\n"
            f"对话 {len(conversation_ids)} 次（{len(friends)} 位朋友）\n"
            f"购买 {purchases} 次\n"
            f"工作 {work} 次\n"
            f"事件 {world_events} 件"
        )

    async def _reflect(self, world_id: str, agent_id: str, digest: str) -> str | None:
        """Ask the provider for a reflection summary via DecisionService."""
        decision_service = self.engine.decision_service
        if decision_service is None:
            return None
        try:
            return await decision_service.run_daily_reflection(world_id, agent_id, digest)
        except Exception:  # noqa: BLE001 - one bad reflection must not block others
            logger.exception("Reflection failed world={} agent={}", world_id, agent_id)
            return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _emit(
            self, session: Session, world_id: str, type_: str, payload: dict
    ) -> None:
        runtime = self.engine.get_runtime(world_id)
        if runtime is None:
            return
        runtime.event_bus.publish(session, runtime.clock.world_time, type_, payload)

    def _world_time(self, session: Session, world_id: str) -> int:
        runtime = self.engine.get_runtime(world_id)
        if runtime is not None:
            return runtime.clock.world_time
        return int(
            session.scalar(select(World.world_time).where(World.world_id == world_id)) or 0
        )


class MemoryRecorder:
    """T6-3: translate observed world events into memories (engine hook)."""

    def __init__(
            self,
            engine: WorldEngine,
            session_factory: sessionmaker,
            memory_service: MemoryService,
    ) -> None:
        self.engine = engine
        self._session_factory = session_factory
        self._memory_service = memory_service

    # ------------------------------------------------------------------ #
    # Event hook
    # ------------------------------------------------------------------ #

    def on_event(self, session: Session, envelope: WorldEventEnvelope) -> None:
        """Record memories for the agents involved in ``envelope``.

        Only events the agent was a party to produce memories (observed info
        only); a conversation between two other agents records nothing for a
        third agent.
        """
        payload = envelope.payload or {}
        event_type = envelope.type
        if event_type == "conversation_message":
            self._on_conversation_message(session, envelope, payload)
        elif event_type == "conversation_ended":
            self._on_conversation_ended(session, envelope, payload)
        elif event_type == "work_completed":
            self._on_work_completed(session, envelope, payload)
        elif event_type == "item_purchased":
            self._on_item(session, envelope, payload, action="purchased")
        elif event_type == "item_sold":
            self._on_item(session, envelope, payload, action="sold")
        elif event_type == "item_used":
            self._on_item_used(session, envelope, payload)
        elif event_type == "money_changed":
            self._on_money_changed(session, envelope, payload)
        elif event_type == "money_transferred":
            self._on_money_transferred(session, envelope, payload)
        elif event_type == "item_given":
            self._on_item_given(session, envelope, payload)
        elif event_type == "world_event_created":
            self._on_world_event(session, envelope, payload)
        elif event_type == "god_action_applied":
            self._on_god_action(session, envelope, payload)
        elif event_type == "structure_built":
            self._on_structure_built(session, envelope, payload)
        elif event_type == "crop_planted":
            self._on_crop_planted(session, envelope, payload)
        elif event_type == "crop_harvested":
            self._on_crop_harvested(session, envelope, payload)

    def record_llm_failure(
            self, session: Session, world_id: str, agent_id: str, reason: str
    ) -> None:
        """Working memory for a failed LLM tool execution (llm_run success=0)."""
        self._memory_service.record(
            session=session,
            world_id=world_id,
            agent_id=agent_id,
            memory_type="working",
            text=f"行动失败：{reason}",
            importance=0.4,
            entities=[],
            keywords=["失败"],
        )

    # ------------------------------------------------------------------ #
    # Per-event rules (T6-3)
    # ------------------------------------------------------------------ #

    def _on_conversation_message(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        world_id = envelope.world_id
        from_id = payload.get("from_agent_id")
        to_id = payload.get("to_agent_id")
        message = payload.get("message") or ""
        if not from_id or not to_id or not message:
            return
        sender_name = self._agent_name(session, world_id, from_id)
        recipient_name = self._agent_name(session, world_id, to_id)
        # The sender remembers what it said; the recipient what it heard.
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=from_id,
            memory_type="episodic", text=f"我对 {recipient_name} 说：{message}",
            importance=0.6, entities=[from_id, to_id],
        )
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=to_id,
            memory_type="episodic", text=f"{sender_name} 对我说：{message}",
            importance=0.6, entities=[from_id, to_id],
        )

    def _on_conversation_ended(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        world_id = envelope.world_id
        conversation = session.get(Conversation, payload.get("conversation_id"))
        if conversation is None:
            return
        agent_a, agent_b = conversation.agent_a, conversation.agent_b
        reason = payload.get("reason") or ""
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=agent_a,
            memory_type="episodic",
            text=f"我和 {self._agent_name(session, world_id, agent_b)} 的对话结束了（{reason}）",
            importance=0.4, entities=[agent_a, agent_b],
        )
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=agent_b,
            memory_type="episodic",
            text=f"我和 {self._agent_name(session, world_id, agent_a)} 的对话结束了（{reason}）",
            importance=0.4, entities=[agent_a, agent_b],
        )

    def _on_work_completed(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        job_name = payload.get("job_name") or payload.get("job_id") or ""
        wage = payload.get("wage") or 0
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic",
            text=f"完成了 {job_name} 工作，获得 {wage} 金币",
            importance=0.5, entities=[payload.get("job_id") or job_name], keywords=["工作"],
        )

    def _on_item(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict, action: str
    ) -> None:
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        item_name = payload.get("item_name") or payload.get("item_id") or ""
        quantity = payload.get("quantity") or 1
        if action == "purchased":
            text = f"购买了 {item_name}×{quantity}"
        else:
            total = payload.get("total") or 0
            text = f"出售了 {item_name}×{quantity}，获得 {total} 金币"
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic", text=text, importance=0.4,
            entities=[payload.get("item_id") or item_name], keywords=["购物"],
        )

    def _on_item_used(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        item_name = payload.get("item_name") or payload.get("item_id") or ""
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="working", text=f"食用了 {item_name}", importance=0.3,
            entities=[payload.get("item_id") or item_name], keywords=["进食"],
        )

    def _on_money_changed(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        agent_id = payload.get("agent_id")
        amount = payload.get("amount") or 0
        if not agent_id or abs(amount) < 30:
            return
        reason = payload.get("reason") or "金钱变化"
        sign = "+" if amount >= 0 else ""
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic",
            text=f"金钱变化：{reason}（{sign}{amount}金币）",
            importance=0.6, entities=[], keywords=["金钱"],
        )

    def _on_money_transferred(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M11: a transfer between two agents — both sides remember it.

        No amount threshold: small transfers are still social events.
        """
        world_id = envelope.world_id
        from_id = payload.get("from_agent_id")
        to_id = payload.get("to_agent_id")
        amount = payload.get("amount") or 0
        if not from_id or not to_id:
            return
        from_name = self._agent_name(session, world_id, from_id)
        to_name = self._agent_name(session, world_id, to_id)
        reason = payload.get("reason") or ""
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=from_id,
            memory_type="episodic",
            text=f"转账给 {to_name} {amount} 金币（{reason}）",
            importance=0.6, entities=[to_id], keywords=["金钱"],
        )
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=to_id,
            memory_type="episodic",
            text=f"收到 {from_name} 转账 {amount} 金币（{reason}）",
            importance=0.6, entities=[from_id], keywords=["金钱"],
        )

    def _on_item_given(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M11: an item gift between two agents — both sides remember it."""
        world_id = envelope.world_id
        from_id = payload.get("from_agent_id")
        to_id = payload.get("to_agent_id")
        quantity = payload.get("quantity") or 1
        if not from_id or not to_id:
            return
        from_name = self._agent_name(session, world_id, from_id)
        to_name = self._agent_name(session, world_id, to_id)
        item_name = payload.get("item_name") or payload.get("item_id") or ""
        reason = payload.get("reason") or ""
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=from_id,
            memory_type="episodic",
            text=f"把 {item_name}×{quantity} 送给了 {to_name}（{reason}）",
            importance=0.6, entities=[to_id], keywords=["物品"],
        )
        self._memory_service.record(
            session=session, world_id=world_id, agent_id=to_id,
            memory_type="episodic",
            text=f"收到 {from_name} 送的 {item_name}×{quantity}（{reason}）",
            importance=0.6, entities=[from_id], keywords=["物品"],
        )

    def _on_world_event(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        agent_id = payload.get("agent_id")
        text = payload.get("text") or ""
        if not text:
            return
        if not agent_id:
            # M7: a public world event (god-created, no agent_id) is witnessed
            # by every agent in the world — episodic 0.6 each.
            agents = session.scalars(
                select(Agent).where(Agent.world_id == envelope.world_id)
            ).all()
            for agent in agents:
                self._memory_service.record(
                    session=session, world_id=envelope.world_id,
                    agent_id=agent.agent_id,
                    memory_type="episodic", text=text, importance=0.6,
                    entities=[], keywords=[],
                )
            return
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic", text=text, importance=0.5,
            entities=[agent_id], keywords=[],
        )

    def _on_god_action(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M7: god_action_applied targeting the agent (episodic 0.7).

        The god_action_applied payload carries ``target_id`` (the affected
        agent) per the M7 contract; commands without a target (pause, weather,
        public events, ...) record nothing here — public events are handled by
        the world_event_created branch instead.
        """
        agent_id = payload.get("agent_id") or payload.get("target_id")
        if not agent_id:
            return
        command_type = payload.get("command_type") or ""
        result = payload.get("result") or {}
        template = _GOD_MEMORY_TEXTS.get(command_type, "受到神谕影响")
        try:
            text = template.format(**result)
        except (KeyError, ValueError, IndexError):  # pragma: no cover - defensive
            text = "受到神谕影响"
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic", text=f"神谕：{text}", importance=0.7,
            entities=[agent_id], keywords=["神谕"],
        )

    def _on_structure_built(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M14: the builder remembers its finished construction (R22.5)."""
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        blueprint_id = payload.get("blueprint_id") or ""
        col = payload.get("col")
        row = payload.get("row")
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic",
            text=f"我建造了 {blueprint_id}（{col},{row}）",
            importance=0.7,
            entities=[agent_id],
            keywords=[blueprint_id, "建造"],
        )

    def _on_crop_planted(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M15: the farmer remembers sowing (R23)."""
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        item_id = payload.get("item_id") or ""
        col = payload.get("col")
        row = payload.get("row")
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic",
            text=f"我在农田（{col},{row}）种下了 {item_id}",
            importance=0.6,
            entities=[agent_id],
            keywords=[item_id, "种植"],
        )

    def _on_crop_harvested(
            self, session: Session, envelope: WorldEventEnvelope, payload: dict
    ) -> None:
        """M15: the farmer remembers the harvest."""
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        item_id = payload.get("item_id") or ""
        products = payload.get("products") or []
        summary = "、".join(
            f"{p.get('item_id')}×{p.get('quantity')}" for p in products
        )
        self._memory_service.record(
            session=session, world_id=envelope.world_id, agent_id=agent_id,
            memory_type="episodic",
            text=f"我收获了 {item_id}，得到 {summary or '（无）'}",
            importance=0.6,
            entities=[agent_id],
            keywords=[item_id, "收获"],
        )

    def _agent_name(self, session: Session, world_id: str, agent_id: str) -> str:
        agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
        return agent.name if agent is not None else agent_id
