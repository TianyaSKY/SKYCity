"""m11_consumption

Revision ID: 7a8901c4ba15
Revises: b7e4a91c03f2
Create Date: 2026-08-05 23:47:47.081464

M12 consumption activation: add mood dimension (agents.mood), productive
item effects (items.mood_restore/work_bonus/yield_bonus), and promo pricing
anchor (store_products.base_sell_price). Additive columns with server
defaults; safe for existing worlds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a8901c4ba15'
down_revision: Union[str, Sequence[str], None] = 'b7e4a91c03f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agents', sa.Column('mood', sa.Integer(), nullable=False, server_default='100'))
    op.add_column('items', sa.Column('mood_restore', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('items', sa.Column('work_bonus', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('items', sa.Column('yield_bonus', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('store_products', sa.Column('base_sell_price', sa.Integer(), nullable=False, server_default='0'))
    op.execute('UPDATE store_products SET base_sell_price = sell_price WHERE base_sell_price = 0')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('store_products', 'base_sell_price')
    op.drop_column('items', 'yield_bonus')
    op.drop_column('items', 'work_bonus')
    op.drop_column('items', 'mood_restore')
    op.drop_column('agents', 'mood')
