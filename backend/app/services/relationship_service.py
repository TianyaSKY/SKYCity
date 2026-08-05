"""RelationshipService: directional, system-computed relationship deltas (M6, T6-5).

One row per (world, source_agent_id, target_agent_id): the source agent's
feelings toward the target. Deltas are derived from observed events by fixed
rules — the LLM never returns relationship values. Every non-zero delta
emits a relationship_changed event carrying the deltas and the resulting
values. familiarity/trust/affection/resentment clamp to 0..100, debt to
0..1000 (debt is reserved, untouched in M6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models.agents import Agent
from app.database.models.conversations import Conversation
from app.database.models.relationships import Relationship
from app.database.models.worlds import World
from app.domain.event import WorldEventEnvelope

if TYPE_CHECKING:  # pragma: no cover - type hints only (engine imports us)
    from app.world_engine.engine import WorldEngine

# Per-intent speaker->listener deltas for a delivered talk message (T6-5).
_INTENT_DELTAS: dict[str, dict[str, int]] = {
    "greet": {"familiarity": 2, "affection": 1},
    "chat": {"familiarity": 2, "affection": 1},
    "offer": {"familiarity": 2, "affection": 1},
    "ask_help": {"familiarity": 2, "affection": 2},
    "leave": {"familiarity": 1, "affection": 0},
}
_DEFAULT_INTENT_DELTAS = {"familiarity": 1, "affection": 0}

# Clamp ranges per axis.
CLAMP_MAIN = (0, 100)
CLAMP_DEBT = (0, 1000)

RELATIONSHIP_AXES = ("familiarity", "trust", "affection", "resentment", "debt")


class RelationshipService:
    """Applies rule deltas to relationship rows; owns the REST listing."""

    def __init__(self, engine: WorldEngine, session_factory: sessionmaker) -> None:
        self.engine = engine
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Event hook
    # ------------------------------------------------------------------ #

    def on_event(self, session: Session, envelope: WorldEventEnvelope) -> None:
        """Apply rule deltas for one event (source = the actor)."""
        world_id = envelope.world_id
        payload = envelope.payload or {}
        event_type = envelope.type
        if event_type == "conversation_message":
            from_id = payload.get("from_agent_id")
            to_id = payload.get("to_agent_id")
            if not from_id or not to_id:
                return
            # The speaker's feelings toward the listener, by intent.
            sender_deltas = _INTENT_DELTAS.get(
                payload.get("intent") or "", _DEFAULT_INTENT_DELTAS
            )
            self.apply_deltas(
                session, world_id, from_id, to_id, sender_deltas,
                world_time=envelope.world_time,
            )
            # The listener noticed being spoken to.
            self.apply_deltas(
                session, world_id, to_id, from_id, {"familiarity": 1},
                world_time=envelope.world_time,
            )
        elif event_type == "conversation_ended":
            if payload.get("reason") != "leave":
                return
            conversation = session.get(Conversation, payload.get("conversation_id"))
            if conversation is None:
                return
            self.apply_deltas(
                session, world_id, conversation.agent_a, conversation.agent_b,
                {"familiarity": 1}, world_time=envelope.world_time,
            )
            self.apply_deltas(
                session, world_id, conversation.agent_b, conversation.agent_a,
                {"familiarity": 1}, world_time=envelope.world_time,
            )
        elif event_type == "god_action_applied":
            # M7 generic hook: the god's favour builds trust toward the actor.
            agent_id = payload.get("agent_id")
            target_id = payload.get("target_agent_id")
            if agent_id and target_id:
                self.apply_deltas(
                    session, world_id, agent_id, target_id, {"trust": 3},
                    world_time=envelope.world_time,
                )
        elif event_type == "item_given":
            # M12 B3: a gift deepens both sides of the relationship (sender
            # feels generous, recipient feels appreciated).
            from_id = payload.get("from_agent_id")
            to_id = payload.get("to_agent_id")
            if not from_id or not to_id:
                return
            self.apply_deltas(
                session, world_id, from_id, to_id,
                {"affection": 3, "familiarity": 2}, world_time=envelope.world_time,
            )
            self.apply_deltas(
                session, world_id, to_id, from_id,
                {"familiarity": 2}, world_time=envelope.world_time,
            )
        # Everything else (money_changed income, work_completed,
        # world_event_created incl. talk rejections, ...) leaves
        # relationships untouched in M6.

    # ------------------------------------------------------------------ #
    # Delta application
    # ------------------------------------------------------------------ #

    def ensure_row(
        self, session: Session, world_id: str, source: str, target: str
    ) -> Relationship:
        """The source->target row, created with zeroes when missing.

        ``session.get`` does not find rows added earlier in the same
        transaction (pending objects), so those are also scanned.
        """
        row = session.get(
            Relationship,
            {"world_id": world_id, "source_agent_id": source, "target_agent_id": target},
        )
        if row is None:
            for pending in session.new:
                if (
                    isinstance(pending, Relationship)
                    and pending.world_id == world_id
                    and pending.source_agent_id == source
                    and pending.target_agent_id == target
                ):
                    return pending
            row = Relationship(
                world_id=world_id,
                source_agent_id=source,
                target_agent_id=target,
                familiarity=0,
                trust=0,
                affection=0,
                resentment=0,
                debt=0,
                updated_at=0,
            )
            session.add(row)
        return row

    def apply_deltas(
        self,
        session: Session,
        world_id: str,
        source: str,
        target: str,
        deltas: dict[str, int],
        world_time: int | None = None,
    ) -> Relationship | None:
        """Apply non-zero ``deltas``; emit relationship_changed.

        Returns the row, or None when every delta was zero (nothing to emit).
        """
        nonzero = {key: value for key, value in deltas.items() if value}
        if not nonzero:
            return None
        row = self.ensure_row(session, world_id, source, target)
        for key, value in nonzero.items():
            if key not in RELATIONSHIP_AXES:
                continue
            lo, hi = CLAMP_DEBT if key == "debt" else CLAMP_MAIN
            setattr(row, key, max(lo, min(hi, int(getattr(row, key)) + int(value))))
        row.updated_at = (
            world_time if world_time is not None else self._world_time(session, world_id)
        )
        self._emit(
            session,
            world_id,
            "relationship_changed",
            {
                "source_agent_id": source,
                "target_agent_id": target,
                "deltas": nonzero,
                "values": {axis: getattr(row, axis) for axis in RELATIONSHIP_AXES},
            },
        )
        return row

    # ------------------------------------------------------------------ #
    # REST
    # ------------------------------------------------------------------ #

    def list_for_agent(self, world_id: str, agent_id: str) -> list[dict]:
        """REST shape: rows where ``agent_id`` is the source; target_name is
        the target's Chinese name."""
        session = self._session_factory()
        try:
            rows = session.scalars(
                select(Relationship)
                .where(
                    Relationship.world_id == world_id,
                    Relationship.source_agent_id == agent_id,
                )
                .order_by(Relationship.target_agent_id)
            ).all()
            names = {
                agent.agent_id: agent.name
                for agent in session.scalars(
                    select(Agent).where(Agent.world_id == world_id)
                )
            }
            return [
                {
                    "source_agent_id": row.source_agent_id,
                    "target_agent_id": row.target_agent_id,
                    "target_name": names.get(row.target_agent_id, row.target_agent_id),
                    "familiarity": row.familiarity,
                    "trust": row.trust,
                    "affection": row.affection,
                    "resentment": row.resentment,
                    "debt": row.debt,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]
        finally:
            session.close()

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
