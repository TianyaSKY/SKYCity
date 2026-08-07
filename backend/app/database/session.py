"""Database session plumbing (M2 adds models; the world engine is sync + SQLite)."""

from collections.abc import Generator

from loguru import logger
from sqlalchemy import create_engine, text as _text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import get_settings

settings = get_settings()

# SQLite is file-local: multiple threads (FastAPI worker threads) may touch the
# same connection; check_same_thread=False is the standard opt-out, and the
# busy timeout avoids "database is locked" under concurrent reads/writes.
_connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if settings.database_url.startswith("sqlite")
    else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models (imported by models package)."""

def initialize_database() -> None:
    """Create every table required by the current ORM model set.

    Migration history is intentionally not supported: a database must be
    created with the current application version.
    """
    from app.database import models  # noqa: F401 - registers all ORM models

    Base.metadata.create_all(engine)
    # A2: existing databases pre-date the partial unique index; create_all
    # never alters old tables, so backfill it here (best-effort — dirty
    # legacy data would fail the DDL and is skipped, the UoW check remains
    # the primary defence).
    with engine.connect() as conn:
        try:
            conn.execute(_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_employment_contract_active_agent "
                "ON employment_contracts (world_id, agent_id) "
                "WHERE status IN ('active', 'on_leave')"
            ))
            conn.commit()
        except Exception:  # noqa: BLE001 - 存量脏数据时跳过，UoW 仍是主防线
            logger.exception("active-contract unique index creation failed")
        # M18: existing databases pre-date the personal-store columns;
        # create_all never alters old tables, so backfill them here
        # (best-effort — a fresh DB already has the columns via the model).
        for alter in (
            "ALTER TABLE stores ADD COLUMN owner_agent_id VARCHAR(64)",
            "ALTER TABLE stores ADD COLUMN name VARCHAR(128)",
        ):
            try:
                conn.execute(_text(alter))
                conn.commit()
            except OperationalError:
                conn.rollback()  # column already exists (fresh DB) — fine
        # M18 R39.4: one store per location for personal shops (the unique
        # index is the last line of defence against concurrent stall grabs).
        try:
            conn.execute(_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_store_location_personal "
                "ON stores (world_id, location_id) "
                "WHERE owner_agent_id IS NOT NULL"
            ))
            conn.commit()
        except Exception:  # noqa: BLE001 - 存量脏数据时跳过，服务层校验仍是主防线
            logger.exception("personal-store location unique index creation failed")


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped session, always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
