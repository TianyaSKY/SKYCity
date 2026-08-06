"""m12_satiety

Revision ID: 9c1e8f2a3b4d
Revises: 7a8901c4ba15
Create Date: 2026-08-06 00:00:00.000000

Rename the hunger stat to satiety (饱食度) with inverted polarity:
0-100, high = full. agents.hunger -> agents.satiety (data inverted via
satiety = 100 - hunger); items.hunger_restore -> items.satiety_restore
(amount unchanged). Event/contract keys (satiety, satiety_before/after)
change at the application layer, not here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c1e8f2a3b4d'
down_revision: Union[str, Sequence[str], None] = '7a8901c4ba15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: rename + invert the stat, rename the item effect."""
    with op.batch_alter_table('agents') as batch_op:
        batch_op.alter_column('hunger', new_column_name='satiety')
    # Invert polarity: old hunger 0 (full) -> satiety 100; 100 (starving) -> 0.
    op.execute('UPDATE agents SET satiety = 100 - satiety')
    with op.batch_alter_table('items') as batch_op:
        batch_op.alter_column('hunger_restore', new_column_name='satiety_restore')


def downgrade() -> None:
    """Downgrade schema: rename back and invert the data again."""
    with op.batch_alter_table('items') as batch_op:
        batch_op.alter_column('satiety_restore', new_column_name='hunger_restore')
    op.execute('UPDATE agents SET satiety = 100 - satiety')
    with op.batch_alter_table('agents') as batch_op:
        batch_op.alter_column('satiety', new_column_name='hunger')
