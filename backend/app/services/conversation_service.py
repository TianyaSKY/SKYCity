"""ConversationService: the M4 rule gate for agent-to-agent talk.

Validates talk requests against the world rules (R1 idle, R2 both idle, R9
manhattan distance <= 3, paused) and runs the conversation lifecycle:
pair-cooldown blocking, MAX_TURNS cap, duplicate detection, priority boost,
and the leave/distance/max_turns/duplicate/timeout end reasons.

E-full conversation lock: starting a conversation occupies both members
(action_type="talk" with a TALK_LOCK_MINUTES hard cap); while locked,
move/wait/sleep/work queue instead of rejecting and fire when the
conversation ends (or at the cap, via ``queued_action`` / ``talk_expired``
scheduler rows). Every state change is persisted to the ``conversations`` /
``conversation_messages`` tables and emitted as a world event
(conversation_started / conversation_message / conversation_ended) through
the runtime's event bus, exactly like the action service.

The service is wired onto WorldEngine.conversation_service (see main.py) so
tools and the decision service reach it through the engine.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config.gameplay import (
    LONELINESS_RELIEF,
    MAX_MESSAGE_CHARS,
    MAX_TURNS,
    PAIR_COOLDOWN_MINUTES,
    TALK_DISTANCE,
    TALK_LOCK_MINUTES,
)
from app.database.models.agents import Agent
from app.database.models.conversations import Conversation, ConversationMessage
from app.database.models.scheduled_actions import ScheduledAction
from app.database.models.worlds import World
from app.domain.event import WorldEventEnvelope
from app.world_engine.engine import WorldEngine, WorldRuntime

# Anti-loop guardrails (M4) — rates/limits live in app.config.gameplay.

# conversation_ended reasons (event contract).
REASON_LEAVE = "leave"
REASON_DISTANCE = "distance"
REASON_MAX_TURNS = "max_turns"
REASON_DUPLICATE = "duplicate"
REASON_TIMEOUT = "timeout"
REASON_COOLDOWN_EXPIRED = "cooldown_expired"
REASON_BOTH_BUSY = "both_busy"

# Validation rejection messages (tool result / 409 reason).
MSG_WORLD_MISSING = "世界不存在"
MSG_AGENT_MISSING = "智能体不存在"
MSG_TARGET_MISSING = "目标智能体不存在"
MSG_SENDER_BUSY = "当前行动未完成"
MSG_TARGET_BUSY = "对方正在忙"
MSG_NOT_NEAR = "对方不在附近"
MSG_PAUSED = "世界已暂停"
MSG_COOLDOWN = "对话冷却中，请稍后再试"
MSG_MAX_TURNS = "对话已达到最大轮数"
MSG_DUPLICATE = "内容重复，对话结束"
MSG_QUEUED = "已排队：对话结束后自动执行"


def manhattan_distance(
        a_col: int, a_row: int, b_col: int, b_row: int
) -> int:
    return abs(a_col - b_col) + abs(a_row - b_row)


class ConversationService:
    """Owns the conversation lifecycle for one world engine."""

    def __init__(
            self, engine: WorldEngine, session_factory: sessionmaker[Session]
    ) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Talk entry point (used by the talk tool and the decision service)
    # ------------------------------------------------------------------ #

    def send_message(
            self,
            world_id: str,
            from_agent_id: str,
            to_agent_id: str,
            message: str,
            intent: str | None,
            trace_id: str | None = None,
    ) -> tuple[bool, str | None, WorldEventEnvelope | None]:
        """Deliver one talk message, enforcing R1/R2/R9 + the anti-loop rules.

        Returns ``(ok, reason, envelope)``; on success ``envelope`` is the
        conversation_message event (conversation_started / conversation_ended
        are also persisted when they apply).
        """
        session = self._session_factory()
        try:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return False, MSG_WORLD_MISSING, None
            world = session.get(World, world_id)
            if world is None:
                return False, MSG_WORLD_MISSING, None
            sender = session.get(Agent, {"world_id": world_id, "agent_id": from_agent_id})
            if sender is None:
                return False, MSG_AGENT_MISSING, None
            target = session.get(Agent, {"world_id": world_id, "agent_id": to_agent_id})
            if target is None:
                return False, MSG_TARGET_MISSING, None
            # E-full: repair stale talk locks (crash / god interrupt) before
            # judging busy-ness, then resolve the pair's active conversation.
            self._repair_stale_locks(session, world_id, sender)
            self._repair_stale_locks(session, world_id, target)
            agent_a, agent_b = sorted([from_agent_id, to_agent_id])
            conversation = self._active_between(session, world_id, agent_a, agent_b)
            if self._busy_reason(sender, conversation) is not None:  # R1
                return False, MSG_SENDER_BUSY, None
            if self._busy_reason(target, conversation) is not None:  # R2
                return False, MSG_TARGET_BUSY, None
            if (
                    manhattan_distance(sender.col, sender.row, target.col, target.row)
                    > TALK_DISTANCE
            ):  # R9
                return False, MSG_NOT_NEAR, None
            if world.paused:
                return False, MSG_PAUSED, None

            message = (message or "").strip()[:MAX_MESSAGE_CHARS]
            intent = (intent or "chat").strip() or "chat"
            world_time = world.world_time

            created = conversation is None
            if created:
                if self._in_cooldown(session, world_id, agent_a, agent_b, world_time):
                    return False, MSG_COOLDOWN, None
                conversation = Conversation(
                    conversation_id=f"conv_{uuid.uuid4().hex[:16]}",
                    world_id=world_id,
                    agent_a=agent_a,
                    agent_b=agent_b,
                    started_at=world_time,
                    turns=0,
                )
                session.add(conversation)
            else:
                if conversation.turns >= MAX_TURNS:
                    self._end(session, runtime, conversation, REASON_MAX_TURNS, world_time, trace_id)
                    session.commit()
                    return False, MSG_MAX_TURNS, None
                if self._is_duplicate(session, conversation, from_agent_id, message):
                    self._end(session, runtime, conversation, REASON_DUPLICATE, world_time, trace_id)
                    session.commit()
                    return False, MSG_DUPLICATE, None

            # E-full: occupy both members with the conversation. A fresh start
            # locks them; a continuing conversation re-asserts the lock so a
            # god interrupt that cleared it is healed on the next message.
            self._lock_conversation(
                session, runtime, conversation, sender, target, world_time
            )

            session.add(
                ConversationMessage(
                    message_id=f"msg_{uuid.uuid4().hex[:16]}",
                    conversation_id=conversation.conversation_id,
                    world_id=world_id,
                    from_agent_id=from_agent_id,
                    to_agent_id=to_agent_id,
                    message=message,
                    intent=intent,
                    sent_at=world_time,
                    read=False,
                )
            )
            conversation.turns += 1

            # R21: a delivered message is social contact — relieve loneliness
            # for both parties and sync the frontend via needs_changed.
            relieved = []
            for _agent in (sender, target):
                if _agent.loneliness > 0:
                    _agent.loneliness = max(0, _agent.loneliness - LONELINESS_RELIEF)
                    relieved.append(_agent)
            for _agent in relieved:
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "needs_changed",
                    {
                        "agent_id": _agent.agent_id,
                        "satiety": _agent.satiety,
                        "energy": _agent.energy,
                        "mood": _agent.mood,
                        "loneliness": _agent.loneliness,
                    },
                    trace_id,
                )

            if created:
                runtime.event_bus.publish(
                    session,
                    world_time,
                    "conversation_started",
                    {
                        "conversation_id": conversation.conversation_id,
                        "agent_ids": [agent_a, agent_b],
                    },
                    trace_id,
                )
            envelope = runtime.event_bus.publish(
                session,
                world_time,
                "conversation_message",
                {
                    "conversation_id": conversation.conversation_id,
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "message": message,
                    "intent": intent,
                },
                trace_id,
            )

            if intent == "leave":
                self._end(session, runtime, conversation, REASON_LEAVE, world_time, trace_id)
            else:
                self._boost_target(session, runtime, world, target, world_time)
            session.commit()
            return True, None, envelope
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Conversation lifecycle helpers
    # ------------------------------------------------------------------ #

    def end_conversation(self, conversation_id: str, reason: str) -> None:
        """Close an active conversation with ``reason`` (idempotent)."""
        session = self._session_factory()
        try:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return
            runtime = self.engine.get_runtime(conversation.world_id)
            if runtime is None:
                return
            self._end(
                session,
                runtime,
                conversation,
                reason,
                runtime.clock.world_time,
                None,
            )
            session.commit()
        finally:
            session.close()

    def end_if_distance_exceeded(self, world_id: str, agent_id: str) -> None:
        """R9: end every active conversation of ``agent_id`` whose partner is
        now more than 3 cells away (called from the move_completed handler
        after the mover's position has been committed)."""
        session = self._session_factory()
        try:
            runtime = self.engine.get_runtime(world_id)
            if runtime is None:
                return
            agent = session.get(Agent, {"world_id": world_id, "agent_id": agent_id})
            if agent is None:
                return
            active = session.scalars(
                select(Conversation).where(
                    Conversation.world_id == world_id,
                    or_(
                        Conversation.agent_a == agent_id,
                        Conversation.agent_b == agent_id,
                    ),
                    Conversation.ended_at.is_(None),
                )
            ).all()
            for conversation in active:
                partner_id = (
                    conversation.agent_b
                    if conversation.agent_a == agent_id
                    else conversation.agent_a
                )
                partner = session.get(
                    Agent, {"world_id": world_id, "agent_id": partner_id}
                )
                if partner is None:
                    continue
                if (
                        manhattan_distance(
                            agent.col, agent.row, partner.col, partner.row
                        )
                        > TALK_DISTANCE
                ):
                    self._end(
                        session,
                        runtime,
                        conversation,
                        REASON_DISTANCE,
                        runtime.clock.world_time,
                        None,
                    )
            session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # History (REST)
    # ------------------------------------------------------------------ #

    def history(
            self, world_id: str, agent_id: str, limit: int = 20
    ) -> list[dict]:
        """Recent conversations involving ``agent_id``, newest first, each with
        its messages (oldest first)."""
        session = self._session_factory()
        try:
            rows = session.scalars(
                select(Conversation)
                .where(
                    Conversation.world_id == world_id,
                    or_(
                        Conversation.agent_a == agent_id,
                        Conversation.agent_b == agent_id,
                    ),
                )
                .order_by(Conversation.started_at.desc(), Conversation.conversation_id.desc())
                .limit(min(max(limit, 1), 500))
            ).all()
            result: list[dict] = []
            for row in rows:
                messages = session.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == row.conversation_id)
                    .order_by(ConversationMessage.sent_at, ConversationMessage.message_id)
                ).all()
                result.append(
                    {
                        "conversation_id": row.conversation_id,
                        "other_agent_id": (
                            row.agent_b if row.agent_a == agent_id else row.agent_a
                        ),
                        "started_at": row.started_at,
                        "ended_at": row.ended_at,
                        "end_reason": row.end_reason,
                        "messages": [
                            {
                                "from_agent_id": m.from_agent_id,
                                "to_agent_id": m.to_agent_id,
                                "message": m.message,
                                "intent": m.intent,
                                "sent_at": m.sent_at,
                            }
                            for m in messages
                        ],
                    }
                )
            return result
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Provider-facing queries (fake provider demo logic)
    # ------------------------------------------------------------------ #

    def active_between(
            self, world_id: str, agent_a: str, agent_b: str
    ) -> Conversation | None:
        """The active conversation between the pair, or None."""
        session = self._session_factory()
        try:
            return self._active_between(
                session, world_id, *sorted([agent_a, agent_b])
            )
        finally:
            session.close()

    def in_cooldown(self, world_id: str, agent_a: str, agent_b: str) -> bool:
        """True when the pair ended a conversation within PAIR_COOLDOWN_MINUTES."""
        session = self._session_factory()
        try:
            world_time = int(
                session.scalar(
                    select(World.world_time).where(World.world_id == world_id)
                )
                or 0
            )
            return self._in_cooldown(
                session, world_id, *sorted([agent_a, agent_b]), world_time
            )
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Internals (single-session building blocks)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _active_between(
            session: Session, world_id: str, agent_a: str, agent_b: str
    ) -> Conversation | None:
        return session.scalar(
            select(Conversation).where(
                Conversation.world_id == world_id,
                Conversation.agent_a == agent_a,
                Conversation.agent_b == agent_b,
                Conversation.ended_at.is_(None),
            )
        )

    @staticmethod
    def _in_cooldown(
            session: Session,
            world_id: str,
            agent_a: str,
            agent_b: str,
            world_time: int,
    ) -> bool:
        latest_end = session.scalar(
            select(func.max(Conversation.ended_at)).where(
                Conversation.world_id == world_id,
                Conversation.agent_a == agent_a,
                Conversation.agent_b == agent_b,
                Conversation.ended_at.is_not(None),
            )
        )
        if latest_end is None:
            return False
        return world_time - latest_end < PAIR_COOLDOWN_MINUTES

    @staticmethod
    def _is_duplicate(
            session: Session, conversation: Conversation, from_agent_id: str, message: str
    ) -> bool:
        """True when the sender already sent the exact same text in this
        conversation (anti-infinite-loop: an agent repeating itself ends it)."""
        return (
                session.scalar(
                    select(func.count())
                    .select_from(ConversationMessage)
                    .where(
                        ConversationMessage.conversation_id == conversation.conversation_id,
                        ConversationMessage.from_agent_id == from_agent_id,
                        ConversationMessage.message == message,
                    )
                )
                or 0
        ) > 0

    def _end(
            self,
            session: Session,
            runtime: WorldRuntime,
            conversation: Conversation,
            reason: str,
            world_time: int,
            trace_id: str | None,
    ) -> None:
        """Close the conversation and emit conversation_ended (idempotent)."""
        if conversation.ended_at is not None:
            return
        conversation.ended_at = world_time
        conversation.end_reason = reason
        runtime.event_bus.publish(
            session,
            world_time,
            "conversation_ended",
            {"conversation_id": conversation.conversation_id, "reason": reason},
            trace_id,
        )
        # E-full: release both members' locks and expedite queued actions so
        # the next tick executes them (leave/max_turns/duplicate/timeout).
        self._unlock_and_fire_queued(session, conversation, world_time)

    def _boost_target(
            self,
            session: Session,
            runtime: WorldRuntime,
            world: World,
            target: Agent,
            world_time: int,
    ) -> None:
        """T4-2 priority boost: an idle target in an autonomous, unpaused world
        gets an agent_decide at world_time + 1 unless a decision is already
        scheduled within the next two minutes.

        The window is deliberately +2, not +1: a decision the agent already
        has at world_time + 2 should absorb the boost, otherwise the agent
        decides twice in a row (boost + stale staggered) and the second,
        redundant decision can make it busy exactly when the partner sends a
        ``leave`` — wedging the conversation open.
        """
        if (
                target.action_type not in (None, "talk")  # E-full: locked targets still reply
                or not world.autonomous
                or world.paused
        ):
            return
        existing = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.world_id == world.world_id,
                ScheduledAction.agent_id == target.agent_id,
                ScheduledAction.action_type == "agent_decide",
                ScheduledAction.due_at <= world_time + 2,
            )
        )
        if existing is not None:
            return
        runtime.scheduler.schedule(
            session,
            target.agent_id,
            "agent_decide",
            world_time + 1,
            {"origin": "conversation_boost"},
        )

    # ------------------------------------------------------------------ #
    # E-full: conversation lock (occupies both members) + queued actions
    # ------------------------------------------------------------------ #

    @staticmethod
    def _busy_reason(agent: Agent, conversation: Conversation | None) -> str | None:
        """Why ``agent`` cannot talk right now (None = free).

        A member locked into ``conversation`` (action_type="talk" with the
        matching conversation_id) is free to keep talking inside it, but busy
        for everyone else — including a brand-new pair (R1/R2).
        """
        if agent.action_type is None:
            return None
        if agent.action_type == "talk":
            conv_id = (agent.action_data or {}).get("conversation_id")
            if conversation is not None and conv_id == conversation.conversation_id:
                return None
        return "busy"

    @staticmethod
    def _clear_talk_lock(agent: Agent) -> None:
        agent.action_type = None
        agent.action_started_at = None
        agent.action_ends_at = None
        agent.action_data = None

    @staticmethod
    def _repair_stale_locks(session: Session, world_id: str, agent: Agent) -> None:
        """Drop a talk lock whose conversation no longer exists or has ended
        (crash, god interrupt, manual DB edit). Keeps the invariant that
        ``action_type="talk"`` always points at an active conversation."""
        if agent.action_type != "talk":
            return
        conv_id = (agent.action_data or {}).get("conversation_id")
        conv = session.get(Conversation, conv_id) if conv_id else None
        if conv is None or conv.ended_at is not None or conv.world_id != world_id:
            ConversationService._clear_talk_lock(agent)

    def _lock_conversation(
            self,
            session: Session,
            runtime: WorldRuntime,
            conversation: Conversation,
            sender: Agent,
            target: Agent,
            world_time: int,
    ) -> None:
        """E-full: occupy both members with the conversation.

        Idempotent: a member already locked into this conversation is left
        untouched, and a member mid-move/work is never clobbered. Every lock
        carries a hard cap (TALK_LOCK_MINUTES) enforced by a durable
        ``talk_expired`` scheduler row.
        """
        for agent in (sender, target):
            if agent.action_type == "talk" and (
                    agent.action_data or {}
            ).get("conversation_id") == conversation.conversation_id:
                continue
            if agent.action_type not in (None, "talk"):
                continue  # mid-move/work: never clobber a real action
            lock_end = world_time + TALK_LOCK_MINUTES
            agent.action_type = "talk"
            agent.action_started_at = world_time
            agent.action_ends_at = lock_end
            agent.action_data = {
                "conversation_id": conversation.conversation_id,
                "reason": "对话中",
            }
            runtime.scheduler.schedule(
                session,
                agent.agent_id,
                "talk_expired",
                lock_end,
                {"conversation_id": conversation.conversation_id},
            )

    def _unlock_and_fire_queued(
            self, session: Session, conversation: Conversation, world_time: int
    ) -> None:
        """Release both members' talk locks and expedite every action queued
        while locked so the next tick executes it."""
        for agent_id in (conversation.agent_a, conversation.agent_b):
            agent = session.get(
                Agent, {"world_id": conversation.world_id, "agent_id": agent_id}
            )
            if (
                    agent is not None
                    and agent.action_type == "talk"
                    and (agent.action_data or {}).get("conversation_id")
                    == conversation.conversation_id
            ):
                self._clear_talk_lock(agent)
            session.execute(
                update(ScheduledAction)
                .where(
                    ScheduledAction.world_id == conversation.world_id,
                    ScheduledAction.agent_id == agent_id,
                    ScheduledAction.action_type == "queued_action",
                )
                .values(due_at=world_time)
            )

    def queue_or_reject(
            self,
            session: Session,
            runtime: WorldRuntime,
            agent: Agent,
            tool_name: str,
            arguments: dict,
    ) -> tuple[bool, str | None]:
        """E-full R1 gate for duration actions (move/wait/sleep/work).

        Locked in a conversation -> the action is queued (one per agent,
        newest replaces oldest) and returns ``(True, MSG_QUEUED)``; it fires
        at the lock's hard cap, or immediately when the conversation ends.
        A stale talk lock is repaired and the caller proceeds normally
        ``(False, None)``.
        """
        if agent.action_type != "talk":
            return False, None
        conv_id = (agent.action_data or {}).get("conversation_id")
        conv = session.get(Conversation, conv_id) if conv_id else None
        if conv is None or conv.ended_at is not None or conv.world_id != agent.world_id:
            self._clear_talk_lock(agent)
            return False, None
        session.execute(
            delete(ScheduledAction).where(
                ScheduledAction.world_id == agent.world_id,
                ScheduledAction.agent_id == agent.agent_id,
                ScheduledAction.action_type == "queued_action",
            )
        )
        due_at = (agent.action_ends_at or runtime.clock.world_time) + 1
        runtime.scheduler.schedule(
            session,
            agent.agent_id,
            "queued_action",
            due_at,
            {"tool": tool_name, "arguments": arguments},
        )
        return True, MSG_QUEUED

    def handle_talk_expired(self, session: Session, action: ScheduledAction) -> None:
        """Scheduler callback for the conversation lock's hard cap: end the
        conversation (if still active) so neither member stays locked forever
        when the LLM goes silent mid-chat."""
        runtime = self.engine.get_runtime(action.world_id)
        if runtime is None:
            return
        conversation = session.get(
            Conversation, action.payload.get("conversation_id") or ""
        )
        if conversation is None or conversation.ended_at is not None:
            return  # already ended via leave/max_turns/duplicate: nothing to do
        self._end(
            session, runtime, conversation, REASON_TIMEOUT, runtime.clock.world_time, None
        )
