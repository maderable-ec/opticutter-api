"""division, the fourth workshop code

Revision ID: 000000000009
Revises: 000000000008
Create Date: 2026-09-17

The shop's own work on a piece gains a fourth service, división, next to
abisagrado, ranurado and ensamble. It is the same kind of code as the other
three (``000000000008``): typed by the seller on the cut list, and any piece
carrying it belongs to the ``worked`` set the additional work is measured on.

One column on each piece table, for the same reason the first three are
columns: the additional work's floors count those pieces in SQL. Nullable, not
backfilled -- no order before this could carry a división -- and no data moves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000009'
down_revision: Union[str, None] = '000000000008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('order_pieces', 'order_placed_pieces')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table, sa.Column('division_code', sa.String(length=32), nullable=True)
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, 'division_code')
