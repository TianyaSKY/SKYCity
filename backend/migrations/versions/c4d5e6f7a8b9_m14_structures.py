"""m14_structures

Revision ID: c4d5e6f7a8b9
Revises: a5b6c7d8e9f0
Create Date: 2026-08-06 12:00:00.000000

M14: agent-built structures (R22) — one row per footprint cell of a placed
blueprint. The composite PK (world_id, col, row) is the occupancy guard for
concurrent builds; status distinguishes in-progress ("building", materials
pre-deducted) from completed ("built") structures.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('tile_structures',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('blueprint_id', sa.String(length=64), nullable=False),
    sa.Column('owner_agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('built_at', sa.Integer(), nullable=True),
    sa.Column('materials_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'owner_agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'col', 'row')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tile_structures')
