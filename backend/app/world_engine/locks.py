"""SQLite write-lock helpers for atomic economy transactions (R4, T5-7).

SQLite has no ``SELECT ... FOR UPDATE``; the atomic pattern is
``BEGIN IMMEDIATE`` (acquires the reserved write lock up front) + conditional
UPDATEs whose WHERE clauses carry the invariant (e.g. ``stock >= qty``), then
commit. A second writer blocks at BEGIN IMMEDIATE and, once the first
commits, its conditional UPDATE matches zero rows -> the caller reports the
race loss (库存不足 / 商店收不下).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

LOCK_RETRIES = 3
LOCK_BACKOFF_SECONDS = 0.02

SessionFactory = Callable[[], Any]


def _is_lock_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc.orig or exc)


@contextmanager
def atomic_write(session_factory: SessionFactory) -> Iterator[Any]:
    """Open a session, ``BEGIN IMMEDIATE``, yield it, commit on clean exit.

    Rolls back on any exception (callers may also roll back themselves and
    raise a sentinel to skip the commit). Callers that need retry-on-lock
    semantics wrap this with :func:`retry_on_lock`.
    """
    session = session_factory()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def retry_on_lock(
        session_factory: sessionmaker,
        fn: Callable[[Any], Any],
        retries: int = LOCK_RETRIES,
        backoff_seconds: float = LOCK_BACKOFF_SECONDS,
) -> Any:
    """Run ``fn(session)`` inside ``BEGIN IMMEDIATE``, retrying up to
    ``retries`` times with backoff when SQLite reports a busy lock.

    The whole transaction body is re-run on retry so a stale read can never
    leak across attempts. Returns fn's return value.
    """
    last_error: OperationalError | None = None
    for attempt in range(retries + 1):
        session = session_factory()
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            result = fn(session)
            session.commit()
            return result
        except OperationalError as exc:
            session.rollback()
            if _is_lock_error(exc) and attempt < retries:
                time.sleep(backoff_seconds)
                last_error = exc
                continue
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    # Unreachable: the final attempt re-raises inside the except branch.
    assert last_error is not None
    raise last_error
