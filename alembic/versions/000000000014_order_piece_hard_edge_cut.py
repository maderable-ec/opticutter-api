"""a piece may opt out of the hard-edge cut

Revision ID: 000000000014
Revises: 000000000013
Create Date: 2026-10-06

Each side with a hard tape is cut 1 mm short (``hard_edges``), and the seller
can now turn that off per piece (``Requirement.hard_edge_cut``). The order's
cut list has to freeze it: the CSV/XML export recomputes the cut size from the
frozen ``edges``, and would otherwise take the millimetre off a piece the
seller asked to cut at its final size.

One boolean on ``order_pieces``, NOT NULL with a server default of true: every
order frozen before this ran under the rule, and no data moves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000014'
down_revision: Union[str, None] = '000000000013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'order_pieces',
        sa.Column(
            'hard_edge_cut',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('order_pieces', 'hard_edge_cut')
