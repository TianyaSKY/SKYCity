"""Run migrations against DATABASE_URL, with transactional SQLite DDL."""
from alembic import context
from sqlalchemy import create_engine

from app.config.settings import get_settings
from app.database.models import Base

config = context.config


def run(connection):
    # sqlite3's legacy transaction mode does not begin a transaction for DDL.
    # Take a write lock before inspecting the legacy schema; errors roll back
    # both DDL and the revision marker instead of leaving a partial upgrade.
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migration is unsupported: the baseline inspects legacy tables.")
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run(supplied)
    else:
        engine = create_engine(get_settings().database_url)
        try:
            with engine.begin() as connection:
                run(connection)
        finally:
            engine.dispose()
