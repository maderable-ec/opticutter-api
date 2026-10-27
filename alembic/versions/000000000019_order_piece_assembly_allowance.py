"""an order piece freezes the ensamble rule

Revision ID: 000000000019
Revises: 000000000018
Create Date: 2026-10-26

The rows that share an assembly code are now the layers of one assembled piece,
cut 5 mm large on each measure and alike (``assembly``), and only the rows the
web marks follow the rule (``Requirement.assembly_allowance``). The order's cut
list has to freeze the mark: the CSV/XML export recomputes the cut size from
the frozen piece, and would otherwise cut an assembly at its final size.

One boolean on ``order_pieces``, NOT NULL with a server default of false: every
order frozen before this ran without the rule, and no data moves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000019'
down_revision: Union[str, None] = '000000000018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'order_pieces',
        sa.Column(
            'assembly_allowance',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('order_pieces', 'assembly_allowance')
