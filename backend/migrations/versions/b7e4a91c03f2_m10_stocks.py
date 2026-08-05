"""m10_stocks

Revision ID: b7e4a91c03f2
Revises: 70b0e584dff7
Create Date: 2026-08-05 17:20:00.000000

M10: the town stock market — one row per listed company (store/job) with
live quote state, plus agent holdings.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4a91c03f2'
down_revision: Union[str, Sequence[str], None] = '70b0e584dff7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('stocks',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('stock_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('company_id', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('base_price', sa.Integer(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=False),
    sa.Column('prev_price', sa.Integer(), nullable=False),
    sa.Column('outstanding_shares', sa.Integer(), nullable=False),
    sa.Column('day_business', sa.Integer(), nullable=False),
    sa.Column('last_div_per_share', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id'], ['worlds.world_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'stock_id')
    )
    op.create_table('stock_holdings',
    sa.Column('world_id', sa.String(length=64), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('stock_id', sa.String(length=64), nullable=False),
    sa.Column('shares', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['world_id', 'agent_id'], ['agents.world_id', 'agents.agent_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['world_id', 'stock_id'], ['stocks.world_id', 'stocks.stock_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('world_id', 'agent_id', 'stock_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('stock_holdings')
    op.drop_table('stocks')
