"""the seller's hand adjustments to a quote's cut plan

Revision ID: 000000000012
Revises: 000000000011
Create Date: 2026-09-23

A pre-order stores inputs, not a plan: it re-optimizes on every read. The
seller's hand adjustments to that plan (``OptimizeRequest.layout_adjustments``:
per pool, which sheets and where each piece sits) are therefore one more input,
stored next to ``variant`` and inherited by the order on confirmation, where the
snapshot freezes the adjusted plan like any other.

Additive and nullable, with nothing to backfill: NULL is "no adjustment", which
is every quote before this.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000012'
down_revision: Union[str, None] = '000000000011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'preorders', sa.Column('layout_adjustments', sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('preorders', 'layout_adjustments')
