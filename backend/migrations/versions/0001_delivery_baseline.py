"""delivery_baseline

Revision ID: 0001
Revises: 
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def _create_table(name, *columns_and_constraints):
    inspector = sa.inspect(op.get_bind())
    if name not in inspector.get_table_names():
        op.create_table(name, *columns_and_constraints)
        return
    expected = {column.name: column for column in columns_and_constraints if isinstance(column, sa.Column)}
    actual = {column["name"]: column for column in inspector.get_columns(name)}
    missing = expected.keys() - actual.keys()
    if missing:
        raise RuntimeError(f"Unsupported legacy schema: {name} is missing {sorted(missing)}. Restore a backup or supply a migration for this version.")
    dialect = op.get_bind().dialect
    primary_keys = set(inspector.get_pk_constraint(name)["constrained_columns"])
    for column_name, column in expected.items():
        stored = actual[column_name]
        if (stored["type"].compile(dialect=dialect) != column.type.compile(dialect=dialect)
                or (not column.nullable and column_name not in primary_keys and stored["nullable"])):
            raise RuntimeError(f"Unsupported legacy column definition: {name}.{column_name}. Inspect the database before upgrading.")


def _create_index(name, table, columns, **kwargs):
    existing = {index["name"]: index for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, **kwargs)
        return
    index = existing[name]
    predicate = kwargs.get("sqlite_where")
    actual_predicate = index.get("dialect_options", {}).get("sqlite_where")
    if (index["column_names"] != columns
            or bool(index["unique"]) != kwargs.get("unique", False)
            or (predicate is not None and str(predicate) != str(actual_predicate))):
        raise RuntimeError(f"Unsupported legacy index definition: {name}. Inspect the database before upgrading.")


def _upgrade_legacy_columns():
    # Frozen list of additions previously performed in session.py. Keep the
    # baseline independent of future ORM changes.
    additions = {
        "stores": [sa.Column("owner_agent_id", sa.String(64)), sa.Column("name", sa.String(128))],
        "items": [sa.Column("work_bonus_jobs", sa.String(256))],
        "worlds": [
            sa.Column("treasury", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("public_work_budget_day", sa.Integer(), nullable=False, server_default="-1"),
            sa.Column("public_work_budget_remaining", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("public_work_escrow", sa.Integer(), nullable=False, server_default="0"),
        ],
        "jobs": [sa.Column("work_kind", sa.String(16), nullable=False, server_default="independent")],
        "store_products": [
            sa.Column("supply_kind", sa.String(16), nullable=False, server_default="local"),
            sa.Column("import_unit_cost", sa.Integer(), nullable=False, server_default="0"),
        ],
        "stocks": [sa.Column("issuer_company_id", sa.String(64))],
        "agents": [sa.Column("goals", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))],
    }
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table, columns in additions.items():
        if table not in tables:
            continue
        present = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in present:
                op.add_column(table, column)


def upgrade():
    _upgrade_legacy_columns()
    _create_table('company_inventories',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('reserved_quantity', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('world_id', 'company_id', 'item_id')
    )
    _create_table('company_transactions',
    sa.Column('transaction_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('balance_after', sa.Integer(), nullable=False),
    sa.Column('related_agent_id', sa.String(length=64), nullable=True),
    sa.Column('related_item_id', sa.String(length=64), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=True),
    sa.Column('reference_type', sa.String(length=32), nullable=False),
    sa.Column('reference_id', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.String(length=256), nullable=False),
    sa.Column('world_time', sa.Integer(), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('transaction_id')
    )
    _create_index('ix_company_transactions_world_company', 'company_transactions', ['world_id', 'company_id'], unique=False)
    _create_table('employment_contracts',
    sa.Column('employment_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('position_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('hired_at', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.Integer(), nullable=False),
    sa.Column('ended_at', sa.Integer(), nullable=True),
    sa.Column('wage_per_shift', sa.Integer(), nullable=False),
    sa.Column('attendance_score', sa.Float(), nullable=False),
    sa.Column('performance_score', sa.Float(), nullable=False),
    sa.Column('completed_shifts', sa.Integer(), nullable=False),
    sa.Column('late_shifts', sa.Integer(), nullable=False),
    sa.Column('absent_shifts', sa.Integer(), nullable=False),
    sa.Column('unpaid_wage', sa.Integer(), nullable=False),
    sa.Column('termination_reason', sa.String(length=512), nullable=True),
    sa.PrimaryKeyConstraint('employment_id')
    )
    _create_index('ix_employment_contracts_world_agent_status', 'employment_contracts', ['world_id', 'agent_id', 'status'], unique=False)
    _create_index('uq_employment_contract_active_agent', 'employment_contracts', ['world_id', 'agent_id'], unique=True, sqlite_where=sa.text("status IN ('active', 'on_leave')"))
    _create_table('job_applications',
    sa.Column('application_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('opening_id', sa.String(length=48), nullable=False),
    sa.Column('position_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('applied_at', sa.Integer(), nullable=False),
    sa.Column('reviewed_at', sa.Integer(), nullable=True),
    sa.Column('reviewed_by_agent_id', sa.String(length=64), nullable=True),
    sa.Column('applicant_reason', sa.String(length=512), nullable=False),
    sa.Column('manager_reason', sa.String(length=512), nullable=True),
    sa.PrimaryKeyConstraint('application_id')
    )
    _create_index('ix_job_applications_world_company_status', 'job_applications', ['world_id', 'company_id', 'status'], unique=False)
    _create_index('uq_job_application_active_opening_agent', 'job_applications', ['world_id', 'opening_id', 'agent_id'], unique=True, sqlite_where=sa.text("status IN ('submitted', 'reviewing')"))
    _create_table('leave_requests',
    sa.Column('request_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('shift_id', sa.String(length=64), nullable=False),
    sa.Column('employment_id', sa.String(length=48), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('reason', sa.String(length=512), nullable=False),
    sa.Column('manager_reason', sa.String(length=512), nullable=True),
    sa.Column('requested_at', sa.Integer(), nullable=False),
    sa.Column('reviewed_at', sa.Integer(), nullable=True),
    sa.Column('reviewed_by_agent_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('request_id')
    )
    _create_index('ix_leave_requests_world_company_status', 'leave_requests', ['world_id', 'company_id', 'status'], unique=False)
    _create_index('ix_leave_requests_world_shift', 'leave_requests', ['world_id', 'shift_id'], unique=False)
    _create_table('procurement_orders',
    sa.Column('order_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('buyer_company_id', sa.String(length=64), nullable=False),
    sa.Column('seller_company_id', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('unit_price', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('filled_at', sa.Integer(), nullable=True),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('order_id')
    )
    _create_index('ix_procurement_orders_world_buyer_seller_item', 'procurement_orders', ['world_id', 'buyer_company_id', 'seller_company_id', 'item_id'], unique=False)
    _create_index('ix_procurement_orders_world_status', 'procurement_orders', ['world_id', 'status'], unique=False)
    _create_table('work_shifts',
    sa.Column('shift_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('employment_id', sa.String(length=48), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('position_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('scheduled_start', sa.Integer(), nullable=False),
    sa.Column('scheduled_end', sa.Integer(), nullable=False),
    sa.Column('actual_start', sa.Integer(), nullable=True),
    sa.Column('actual_end', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('late_minutes', sa.Integer(), nullable=False),
    sa.Column('worked_minutes', sa.Integer(), nullable=False),
    sa.Column('wage_due', sa.Integer(), nullable=False),
    sa.Column('wage_paid', sa.Integer(), nullable=False),
    sa.Column('payroll_status', sa.String(length=24), nullable=False),
    sa.Column('output_json', sa.JSON(), nullable=False),
    sa.Column('absence_reason', sa.String(length=256), nullable=True),
    sa.PrimaryKeyConstraint('shift_id'),
    sa.UniqueConstraint('world_id', 'employment_id', 'scheduled_start', name='uq_work_shift_contract_start')
    )
    _create_index('ix_work_shifts_world_agent_status', 'work_shifts', ['world_id', 'agent_id', 'status'], unique=False)
    _create_table('worlds',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('world_time', sa.Integer(), nullable=False),
    sa.Column('speed', sa.Integer(), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('weather', sa.String(length=16), nullable=False),
    sa.Column('autonomous', sa.Boolean(), nullable=False),
    sa.Column('treasury', sa.Integer(), nullable=False),
    sa.Column('public_work_budget_day', sa.Integer(), nullable=False),
    sa.Column('public_work_budget_remaining', sa.Integer(), nullable=False),
    sa.Column('public_work_escrow', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('world_id')
    )
    _create_table('agents',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('age', sa.Integer(), nullable=False),
    sa.Column('occupation', sa.String(length=64), nullable=False),
    sa.Column('background', sa.String(length=512), nullable=False),
    sa.Column('values', sa.JSON(), nullable=False),
    sa.Column('long_term_goals', sa.JSON(), nullable=False),
    sa.Column('goals', sa.JSON(), nullable=False),
    sa.Column('speaking_style', sa.String(length=128), nullable=False),
    sa.Column('personality', sa.JSON(), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('location_id', sa.String(length=64), nullable=True),
    sa.Column('satiety', sa.Integer(), nullable=False),
    sa.Column('energy', sa.Integer(), nullable=False),
    sa.Column('mood', sa.Integer(), nullable=False),
    sa.Column('loneliness', sa.Integer(), nullable=False),
    sa.Column('money', sa.Integer(), nullable=False),
    sa.Column('action_type', sa.String(length=16), nullable=True),
    sa.Column('action_started_at', sa.Integer(), nullable=True),
    sa.Column('action_ends_at', sa.Integer(), nullable=True),
    sa.Column('action_data', sa.JSON(), nullable=True),
    sa.Column('is_deciding', sa.Boolean(), nullable=False),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('last_decision_at', sa.Integer(), nullable=True),
    sa.Column('daily_token_usage', sa.Integer(), nullable=False),
    sa.Column('daily_call_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'agent_id')
    )
    _create_table('companies',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=96), nullable=False),
    sa.Column('company_type', sa.String(length=32), nullable=False),
    sa.Column('location_id', sa.String(length=64), nullable=False),
    sa.Column('owner_agent_id', sa.String(length=64), nullable=True),
    sa.Column('manager_agent_id', sa.String(length=64), nullable=True),
    sa.Column('money', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('founded_at', sa.Integer(), nullable=False),
    sa.Column('suspended_at', sa.Integer(), nullable=True),
    sa.Column('closed_at', sa.Integer(), nullable=True),
    sa.Column('consecutive_loss_days', sa.Integer(), nullable=False),
    sa.Column('unpaid_wage_total', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'company_id')
    )
    _create_table('conversations',
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_a', sa.String(length=64), nullable=False),
    sa.Column('agent_b', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.Integer(), nullable=False),
    sa.Column('ended_at', sa.Integer(), nullable=True),
    sa.Column('end_reason', sa.String(length=32), nullable=True),
    sa.Column('turns', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('conversation_id')
    )
    _create_index('ix_conversations_world_agent_a', 'conversations', ['world_id', 'agent_a'], unique=False)
    _create_index('ix_conversations_world_agent_b', 'conversations', ['world_id', 'agent_b'], unique=False)
    _create_table('god_actions',
    sa.Column('command_id', sa.String(length=64), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('command_type', sa.String(length=32), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('parameters_json', sa.JSON(), nullable=False),
    sa.Column('reason', sa.String(length=256), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('result_json', sa.JSON(), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('command_id')
    )
    _create_index('ix_god_actions_world_created', 'god_actions', ['world_id', 'created_at'], unique=False)
    _create_table('items',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('satiety_restore', sa.Integer(), nullable=False),
    sa.Column('mood_restore', sa.Integer(), nullable=False),
    sa.Column('work_bonus', sa.Integer(), nullable=False),
    sa.Column('work_bonus_jobs', sa.String(length=256), nullable=True),
    sa.Column('yield_bonus', sa.Integer(), nullable=False),
    sa.Column('base_price', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'item_id')
    )
    _create_table('jobs',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('location_id', sa.String(length=64), nullable=False),
    sa.Column('interactable_id', sa.String(length=64), nullable=False),
    sa.Column('duration_minutes', sa.Integer(), nullable=False),
    sa.Column('wage', sa.Integer(), nullable=False),
    sa.Column('energy_cost_per_hour', sa.Integer(), nullable=False),
    sa.Column('products_json', sa.JSON(), nullable=False),
    sa.Column('work_kind', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'job_id')
    )
    _create_table('llm_runs',
    sa.Column('run_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('world_time', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('tool_name', sa.String(length=32), nullable=False),
    sa.Column('tool_arguments', sa.JSON(), nullable=False),
    sa.Column('tool_result', sa.JSON(), nullable=False),
    sa.Column('success', sa.Integer(), nullable=False),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('raw_summary', sa.String(length=512), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id')
    )
    _create_index('ix_llm_runs_world_agent', 'llm_runs', ['world_id', 'agent_id'], unique=False)
    _create_table('locations',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('location_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('location_type', sa.String(length=32), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.Column('open_hour', sa.Integer(), nullable=False),
    sa.Column('close_hour', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'location_id')
    )
    _create_table('memories',
    sa.Column('memory_id', sa.String(length=64), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('memory_type', sa.String(length=16), nullable=False),
    sa.Column('text', sa.String(length=512), nullable=False),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('entities_json', sa.JSON(), nullable=False),
    sa.Column('keywords_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('last_recalled_at', sa.Integer(), nullable=True),
    sa.Column('recall_count', sa.Integer(), nullable=False),
    sa.Column('resolved', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('memory_id')
    )
    _create_index('ix_memories_world_agent', 'memories', ['world_id', 'agent_id'], unique=False)
    _create_table('relationships',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('source_agent_id', sa.String(length=64), nullable=False),
    sa.Column('target_agent_id', sa.String(length=64), nullable=False),
    sa.Column('familiarity', sa.Integer(), nullable=False),
    sa.Column('trust', sa.Integer(), nullable=False),
    sa.Column('affection', sa.Integer(), nullable=False),
    sa.Column('resentment', sa.Integer(), nullable=False),
    sa.Column('debt', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'source_agent_id', 'target_agent_id')
    )
    _create_table('saves',
    sa.Column('save_id', sa.String(length=64), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('payload_json', sa.JSON(), nullable=False),
    sa.Column('map_version', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('save_id')
    )
    _create_table('scheduled_actions',
    sa.Column('action_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('action_type', sa.String(length=32), nullable=False),
    sa.Column('due_at', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('action_id')
    )
    _create_index('ix_scheduled_world_due', 'scheduled_actions', ['world_id', 'due_at'], unique=False)
    _create_table('stocks',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('stock_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('issuer_company_id', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('base_price', sa.Integer(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=False),
    sa.Column('outstanding_shares', sa.Integer(), nullable=False),
    sa.Column('day_business', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'stock_id')
    )
    _create_table('stores',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('store_id', sa.String(length=64), nullable=False),
    sa.Column('location_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=True),
    sa.Column('owner_agent_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=128), nullable=True),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'store_id')
    )
    _create_table('transactions',
    sa.Column('tx_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('balance_after', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=256), nullable=False),
    sa.Column('world_time', sa.Integer(), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tx_id')
    )
    _create_index('ix_transactions_world_agent', 'transactions', ['world_id', 'agent_id'], unique=False)
    _create_table('world_events',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('world_time', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=48), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('trace_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'event_id'),
    sa.UniqueConstraint('world_id', 'sequence', name='uq_world_events_world_sequence')
    )
    _create_index('ix_world_events_world_seq', 'world_events', ['world_id', 'sequence'], unique=False)
    _create_table('conversation_messages',
    sa.Column('message_id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('from_agent_id', sa.String(length=64), nullable=False),
    sa.Column('to_agent_id', sa.String(length=64), nullable=False),
    sa.Column('message', sa.String(length=256), nullable=False),
    sa.Column('intent', sa.String(length=16), nullable=False),
    sa.Column('sent_at', sa.Integer(), nullable=False),
    sa.Column('read', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.conversation_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('message_id')
    )
    _create_index('ix_conversation_messages_conversation_id', 'conversation_messages', ['conversation_id'], unique=False)
    _create_index('ix_conversation_messages_to_read', 'conversation_messages', ['to_agent_id', 'read'], unique=False)
    _create_table('crops',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('planted_by', sa.String(length=64), nullable=False),
    sa.Column('planted_at', sa.Integer(), nullable=False),
    sa.Column('stage', sa.Integer(), nullable=False),
    sa.Column('next_stage_at', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['world_id', 'planted_by'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'col', 'row')
    )
    _create_table('employments',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('hours_worked', sa.Float(), nullable=False),
    sa.Column('total_earned', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'job_id'], ['jobs.world_id', 'jobs.job_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'agent_id', 'job_id')
    )
    _create_table('inventories',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'item_id'], ['items.world_id', 'items.item_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'agent_id', 'item_id')
    )
    _create_table('positions',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('position_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=64), nullable=False),
    sa.Column('description', sa.String(length=256), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.Column('wage_per_shift', sa.Integer(), nullable=False),
    sa.Column('shift_start_minute', sa.Integer(), nullable=False),
    sa.Column('shift_end_minute', sa.Integer(), nullable=False),
    sa.Column('working_days_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'company_id'], ['companies.world_id', 'companies.company_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'position_id')
    )
    _create_table('stock_holdings',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('stock_id', sa.String(length=64), nullable=False),
    sa.Column('shares', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'stock_id'], ['stocks.world_id', 'stocks.stock_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'agent_id', 'stock_id')
    )
    _create_table('store_products',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('store_id', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('sell_price', sa.Integer(), nullable=False),
    sa.Column('base_sell_price', sa.Integer(), nullable=False),
    sa.Column('buy_price', sa.Integer(), nullable=False),
    sa.Column('stock', sa.Integer(), nullable=False),
    sa.Column('stock_cap', sa.Integer(), nullable=False),
    sa.Column('restock_daily', sa.Integer(), nullable=False),
    sa.Column('supply_kind', sa.String(length=16), nullable=False),
    sa.Column('import_unit_cost', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'item_id'], ['items.world_id', 'items.item_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'store_id'], ['stores.world_id', 'stores.store_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'store_id', 'item_id')
    )
    _create_table('tile_structures',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('blueprint_id', sa.String(length=64), nullable=False),
    sa.Column('owner_agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('built_at', sa.Integer(), nullable=True),
    sa.Column('materials_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'owner_agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'col', 'row')
    )
    _create_table('job_openings',
    sa.Column('opening_id', sa.String(length=48), nullable=False),
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('position_id', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('vacancies', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('opened_at', sa.Integer(), nullable=False),
    sa.Column('closes_at', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['world_id', 'position_id'], ['positions.world_id', 'positions.position_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('opening_id')
    )
    _create_index(op.f('ix_job_openings_world_id'), 'job_openings', ['world_id'], unique=False)

    _create_index("uq_store_location_personal", "stores", ["world_id", "location_id"], unique=True, sqlite_where=sa.text("owner_agent_id IS NOT NULL"))
    _create_index("uq_store_owner_personal", "stores", ["world_id", "owner_agent_id"], unique=True, sqlite_where=sa.text("owner_agent_id IS NOT NULL"))


def downgrade():
    raise RuntimeError("The delivery baseline cannot be downgraded safely. Restore the pre-upgrade database backup with its matching application version.")
