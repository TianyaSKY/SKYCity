"""m8_stability

Revision ID: 48adfa58a971
Revises: 3cf9f98ff681
Create Date: 2026-08-05 16:00:00.000000

M8: per-agent decision observability + daily cost counters. SQLite needs a
server_default for NOT NULL columns added to a non-empty table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '48adfa58a971'
down_revision: Union[str, Sequence[str], None] = '3cf9f98ff681'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agents', sa.Column('last_decision_at', sa.Integer(), nullable=True))
    op.add_column('agents', sa.Column('daily_token_usage', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.add_column('agents', sa.Column('daily_call_count', sa.Integer(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agents', 'daily_call_count')
    op.drop_column('agents', 'daily_token_usage')
    op.drop_column('agents', 'last_decision_at')
