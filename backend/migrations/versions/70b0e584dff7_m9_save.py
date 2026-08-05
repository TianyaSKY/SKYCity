"""m9_save

Revision ID: 70b0e584dff7
Revises: 48adfa58a971
Create Date: 2026-08-05 16:40:08.822888

M9: save/restore/replay — one row per archived world snapshot; the payload
holds the full serialized state (docs/world-rules.md R17).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70b0e584dff7'
down_revision: Union[str, Sequence[str], None] = '48adfa58a971'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'saves',
        sa.Column('save_id', sa.String(length=64), nullable=False),
        sa.Column('world_id', sa.String(length=64), nullable=False),
        sa.Column('payload_json', sa.JSON(), nullable=False),
        sa.Column('map_version', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('save_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('saves')
