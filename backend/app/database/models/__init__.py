"""Database models package.

Placeholder for M2: model classes (worlds, agents, ...) will be added here.
The declarative Base is re-exported so alembic and future models import from one place.
"""

from app.database.session import Base

__all__ = ["Base"]
