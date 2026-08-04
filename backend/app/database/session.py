"""Database session plumbing (M2 adds models; the world engine is sync + SQLite)."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import get_settings

settings = get_settings()

# SQLite is file-local: multiple threads (FastAPI worker threads) may touch the
# same connection; check_same_thread=False is the standard opt-out.
_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
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


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped session, always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
