"""Release gates: fresh installs, legacy data, rollback and schema drift."""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect, text

from app.database.migrations import database_revision, migration_config, upgrade_database
from app.database.models import Base


@pytest.fixture
def migration_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    yield engine
    engine.dispose()


def test_fresh_upgrade_is_idempotent_and_matches_models(migration_engine):
    upgrade_database(migration_engine)
    upgrade_database(migration_engine)
    assert database_revision(migration_engine) == "0001"
    with migration_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_unversioned_current_database_keeps_world(migration_engine):
    Base.metadata.create_all(migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(Base.metadata.tables['worlds'].insert().values(world_id='legacy', name='旧世界'))
    upgrade_database(migration_engine)
    with migration_engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM worlds WHERE world_id='legacy'")).scalar_one() == '旧世界'
    assert database_revision(migration_engine) == "0001"


def test_legacy_missing_columns_are_backfilled(migration_engine):
    with migration_engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE worlds (
            world_id VARCHAR(64) PRIMARY KEY, name VARCHAR(128) NOT NULL,
            world_time INTEGER NOT NULL, speed INTEGER NOT NULL,
            paused BOOLEAN NOT NULL, weather VARCHAR(16) NOT NULL,
            autonomous BOOLEAN NOT NULL, created_at DATETIME NOT NULL)""")
        connection.exec_driver_sql("INSERT INTO worlds VALUES ('old', '旧世界', 600, 2, 1, 'clear', 0, '2026-01-01')")
    upgrade_database(migration_engine)
    with migration_engine.connect() as connection:
        row = connection.execute(text("SELECT name, world_time, treasury, public_work_budget_day, public_work_escrow FROM worlds")).one()
        assert tuple(row) == ('旧世界', 600, 0, -1, 0)


def test_all_former_startup_additions_are_migrated(migration_engine):
    Base.metadata.create_all(migration_engine)
    additions = {
        'stores': ['owner_agent_id', 'name'],
        'items': ['work_bonus_jobs'],
        'worlds': ['treasury', 'public_work_budget_day', 'public_work_budget_remaining', 'public_work_escrow'],
        'jobs': ['work_kind'],
        'store_products': ['supply_kind', 'import_unit_cost'],
        'stocks': ['issuer_company_id'],
        'agents': ['goals'],
    }
    with migration_engine.begin() as connection:
        connection.exec_driver_sql('DROP INDEX uq_store_owner_personal')
        connection.exec_driver_sql('DROP INDEX uq_store_location_personal')
        for table, columns in additions.items():
            for column in columns:
                connection.exec_driver_sql(f'ALTER TABLE {table} DROP COLUMN {column}')
    upgrade_database(migration_engine)
    with migration_engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_unsupported_legacy_schema_rolls_back(migration_engine):
    with migration_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE worlds (world_id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO worlds VALUES ('saved')")
    with pytest.raises(RuntimeError, match='Unsupported legacy schema'):
        upgrade_database(migration_engine)
    assert inspect(migration_engine).get_table_names() == ['worlds']
    assert [c['name'] for c in inspect(migration_engine).get_columns('worlds')] == ['world_id']
    with migration_engine.connect() as connection:
        assert connection.execute(text('SELECT world_id FROM worlds')).scalar_one() == 'saved'


def test_duplicate_legacy_stalls_block_upgrade_and_preserve_data(migration_engine):
    Base.metadata.create_all(migration_engine)
    with migration_engine.begin() as connection:
        connection.exec_driver_sql('DROP INDEX uq_store_owner_personal')
        connection.execute(Base.metadata.tables['stores'].insert(), [
            dict(world_id='w', store_id='a', location_id='a', owner_agent_id='same'),
            dict(world_id='w', store_id='b', location_id='b', owner_agent_id='same'),
        ])
    with pytest.raises(Exception, match='UNIQUE constraint failed'):
        upgrade_database(migration_engine)
    assert database_revision(migration_engine) is None
    with migration_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM stores')).scalar_one() == 2


def test_baseline_downgrade_refuses_data_loss(migration_engine):
    upgrade_database(migration_engine)
    with pytest.raises(RuntimeError, match='cannot be downgraded safely'):
        with migration_engine.begin() as connection:
            config = migration_config()
            config.attributes['connection'] = connection
            command.downgrade(config, 'base')
    assert database_revision(migration_engine) == '0001'


def test_database_from_unknown_newer_version_is_rejected(migration_engine):
    with migration_engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('future_release')")
    with pytest.raises(CommandError, match='future_release'):
        upgrade_database(migration_engine)
    assert database_revision(migration_engine) == 'future_release'
    assert inspect(migration_engine).get_table_names() == ['alembic_version']
