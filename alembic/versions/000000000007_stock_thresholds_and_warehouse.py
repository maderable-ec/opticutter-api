"""low-stock thresholds and the branch's vendor warehouse

Revision ID: 000000000007
Revises: 000000000006
Create Date: 2026-09-16

Three columns for one feature: reading the vendor's per-warehouse stock
(``binventario``) so a quote can warn about material that is running out.

``settings.stock_threshold_board`` / ``stock_threshold_edge_banding`` are
"how low is low", and there are two of them because the UNITS are not the same:
the vendor counts boards in whole SHEETS and edge banding in LINEAR METRES (the
live catalog holds 558 sheets of the best-selling board and 19818 metres of
white tape), so a single number could never mean anything for both. They carry a
``server_default`` because the singleton row already exists in every deployed
database and a NULL there would break the first read; the default is the same
value ``config`` seeds a fresh row with.

``branches.warehouse_code`` is the vendor's ``mbodega.cod`` -- what
``binventario.bod`` points at (1 = Bodega local/Sucúa, 2 = Macas). A dedicated
column and not ``branches.code``, which is free text the admin can edit from the
dashboard: the day somebody renames "SUCUA" the inventory would quietly start
answering for the wrong warehouse.

Nullable and deliberately NOT backfilled here. Branch rows are data, and this
project seeds data from ``scripts/seed_settings.py`` (idempotent, upserts by
``code``), never from a migration -- matching by ``code`` inside a migration
would hardcode the very string this column exists to stop depending on. NULL is
a meaningful value in its own right: "this branch does not consult stock", which
keeps the alert quiet and the report skipping it instead of inventing a zero.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000007'
down_revision: Union[str, None] = '000000000006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'settings',
        sa.Column(
            'stock_threshold_board',
            sa.Float(),
            nullable=False,
            server_default='5',
        ),
    )
    op.add_column(
        'settings',
        sa.Column(
            'stock_threshold_edge_banding',
            sa.Float(),
            nullable=False,
            server_default='50',
        ),
    )
    op.add_column(
        'branches', sa.Column('warehouse_code', sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('branches', 'warehouse_code')
    op.drop_column('settings', 'stock_threshold_edge_banding')
    op.drop_column('settings', 'stock_threshold_board')
