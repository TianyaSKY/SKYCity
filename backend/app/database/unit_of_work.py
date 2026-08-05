"""Unit of work wrapper for the economy service (R4 atomicity, T5-7).

Small, deliberately boring: exposes ``run(fn)`` which executes ``fn(session)``
inside a SQLite ``BEGIN IMMEDIATE`` transaction with bounded retry on lock
contention, and commits/rolls back exactly once. The conditional-UPDATE guard
inside fn is the real race protection; the retry only absorbs transient
"database is locked" failures.
"""

from __future__ import annotations

from typing import Any, Callable

from app.world_engine.locks import retry_on_lock


class UnitOfWork:
    """Retry-on-lock transaction runner bound to one session factory."""

    def __init__(self, session_factory, retries: int = 3, backoff_seconds: float = 0.02) -> None:
        self._session_factory = session_factory
        self._retries = retries
        self._backoff_seconds = backoff_seconds

    def run(self, fn: Callable[[Any], Any]) -> Any:
        """Execute ``fn(session)`` atomically, returning its result.

        The transaction is retried up to ``retries`` times when SQLite reports
        a busy lock; other errors roll back and propagate.
        """
        return retry_on_lock(
            self._session_factory,
            fn,
            retries=self._retries,
            backoff_seconds=self._backoff_seconds,
        )
