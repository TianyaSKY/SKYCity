"""EventBus: allocates per-world sequences, persists envelopes, queues WS pushes.

One EventBus per world runtime. ``publish`` is synchronous (fast SQLite write
inside the caller's transaction); the envelope is also queued in
``pending`` so the engine can flush it to WebSocket clients on the event loop.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models.world_events import WorldEvent
from app.domain.event import WorldEventEnvelope


def _event_id(sequence: int) -> str:
    return f"evt_{sequence:06d}"


def _trace_id(sequence: int) -> str:
    return f"trc_{sequence:06d}"


class EventBus:
    """Per-world event sequence allocator + persistence + pending queue."""

    def __init__(self, world_id: str) -> None:
        self.world_id = world_id
        self._sequence = 0
        self._pending: list[WorldEventEnvelope] = []
        # M6: engine hook fired for every published envelope (memories,
        # relationships); must be idempotent for derived event types.
        self.on_publish: Callable[[Session, WorldEventEnvelope], None] | None = None

    # ------------------------------------------------------------------ #
    # Sequence
    # ------------------------------------------------------------------ #

    @property
    def sequence(self) -> int:
        """Highest sequence allocated for this world (persisted)."""
        return self._sequence

    @property
    def pending(self) -> list[WorldEventEnvelope]:
        """Envelopes produced but not yet flushed to WebSocket clients."""
        return self._pending

    def take_pending(self) -> list[WorldEventEnvelope]:
        """Atomically drain the pending queue (called by the engine's flush)."""
        envelopes = self._pending
        self._pending = []
        return envelopes

    def init_sequence(self, session: Session) -> None:
        """Restore the counter from the highest persisted sequence (replay-safe)."""
        current = session.scalar(
            select(func.max(WorldEvent.sequence)).where(WorldEvent.world_id == self.world_id)
        )
        self._sequence = int(current or 0)

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #

    def publish(
        self,
        session: Session,
        world_time: int,
        type_: str,
        payload: dict | None = None,
        trace_id: str | None = None,
    ) -> WorldEventEnvelope:
        """Allocate the next sequence, persist the row, queue the push.

        The row is added to ``session`` (caller commits); the envelope is also
        queued for the next WS flush.
        """
        self._sequence += 1
        sequence = self._sequence
        envelope = WorldEventEnvelope(
            event_id=_event_id(sequence),
            sequence=sequence,
            world_id=self.world_id,
            world_time=world_time,
            type=type_,
            payload=payload or {},
            trace_id=trace_id if trace_id is not None else _trace_id(sequence),
        )
        session.add(
            WorldEvent(
                world_id=self.world_id,
                event_id=envelope.event_id,
                sequence=sequence,
                world_time=world_time,
                type=type_,
                payload=envelope.payload,
                trace_id=envelope.trace_id,
            )
        )
        self._pending.append(envelope)
        if self.on_publish is not None:
            self.on_publish(session, envelope)
        return envelope
