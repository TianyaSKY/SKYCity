"""m15_crops

Revision ID: d6e7f8a9b0c1
Revises: c4d5e6f7a8b9
Create Date: 2026-08-06 22:00:00.000000

M15: planted crops (R23) — one row per farm cell from planting until harvest.
The composite PK (world_id, col, row) is the occupancy guard; growth is
scheduler-driven (crop_grow callbacks, stage + next_stage_at on the row).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('crops',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('col', sa.Integer(), nullable=False),
    sa.Column('row', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.String(length=64), nullable=False),
    sa.Column('planted_by', sa.String(length=64), nullable=False),
    sa.Column('planted_at', sa.Integer(), nullable=False),
    sa.Column('stage', sa.Integer(), nullable=False),
    sa.Column('next_stage_at', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'planted_by'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'col', 'row')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('crops')
