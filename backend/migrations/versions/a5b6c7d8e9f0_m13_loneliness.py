"""m13_loneliness

Revision ID: a5b6c7d8e9f0
Revises: 9c1e8f2a3b4d
Create Date: 2026-08-06 00:10:00.000000

R21 loneliness dimension: add agents.loneliness (0-100, high = lonely),
starts at 0, rises hourly, relieved by talk messages. Additive column with
server default; safe for existing worlds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5b6c7d8e9f0'
down_revision: Union[str, Sequence[str], None] = '9c1e8f2a3b4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agents', sa.Column('loneliness', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agents', 'loneliness')
