"""Scheduler: the per-world queue of due world-engine callbacks.

Rows live in ``scheduled_actions`` (durable). Each tick the engine asks the
scheduler for actions with ``due_at <= world_time`` and dispatches them to a
handler registered by action type (move_completed, wait_completed,
capacity_recheck, ...). Handlers mutate DB state and publish events via the
runtime's event bus.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database.models.scheduled_actions import ScheduledAction

Handler = Callable[[Session, ScheduledAction], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    """Owns the scheduled_actions queue for exactly one world."""

    def __init__(self, world_id: str) -> None:
        self.world_id = world_id
        self._handlers: dict[str, Handler] = {}

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    def register(self, action_type: str, handler: Handler) -> None:
        """Bind a handler for a scheduled action type."""
        self._handlers[action_type] = handler

    # ------------------------------------------------------------------ #
    # Queue operations
    # ------------------------------------------------------------------ #

    def schedule(
        self,
        session: Session,
        agent_id: str,
        action_type: str,
        due_at: int,
        payload: dict | None = None,
    ) -> ScheduledAction:
        """Enqueue a callback to fire at world_time ``due_at``."""
        row = ScheduledAction(
            world_id=self.world_id,
            agent_id=agent_id,
            action_type=action_type,
            due_at=due_at,
            payload=payload or {},
            created_at=_utcnow(),
        )
        session.add(row)
        return row

    def cancel_for_agent(self, session: Session, agent_id: str) -> None:
        """Drop every pending callback for an agent (e.g. wait interrupted)."""
        session.execute(
            delete(ScheduledAction).where(
                ScheduledAction.world_id == self.world_id,
                ScheduledAction.agent_id == agent_id,
            )
        )

    def load_due(self, session: Session, world_time: int) -> list[ScheduledAction]:
        """All callbacks due at or before ``world_time``, in fire order."""
        stmt = (
            select(ScheduledAction)
            .where(
                ScheduledAction.world_id == self.world_id,
                ScheduledAction.due_at <= world_time,
            )
            .order_by(ScheduledAction.due_at, ScheduledAction.created_at)
        )
        return list(session.scalars(stmt))

    def dispatch(self, session: Session, action: ScheduledAction) -> None:
        """Run the handler for ``action`` (if any) and consume the row.

        The row is deleted BEFORE the handler runs so handler-side queue
        checks (``has_pending``) never see the just-fired action as pending.
        Handlers only read the action's Python attributes, not its DB row.
        """
        session.delete(action)
        handler = self._handlers.get(action.action_type)
        if handler is not None:
            handler(session, action)
