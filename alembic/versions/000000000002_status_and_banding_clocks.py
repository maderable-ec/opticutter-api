"""status and banding clocks

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-09-07

Two denormalized timestamps on ``orders`` so the listing and the shop-floor board
can say how long an order has been sitting where it is -- the signal the office
pushes people with. Both follow the pattern ``queued_at`` already set: written by
the service at the single point that owns the change, read as one column instead
of a per-row walk of ``order_status_history``.

The backfills are the whole reason this is not just two ``add_column`` calls; see
the comments on each.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000002'
down_revision: Union[str, None] = '000000000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The two backfills live as module constants so the suite can run them against
# real rows (``tests/test_order_clock_backfill.py``). An ``op.execute`` string is
# otherwise only ever exercised on an empty database, where both are no-ops --
# and both of these turn on a condition that is easy to get wrong and silent when
# wrong.

# When did each order enter the status it is in now? The latest history row that
# landed on that status.
#
# ``from_status <> to_status`` is load-bearing: ``set_priority`` and
# ``change_branch`` record themselves as history rows with from == to (they are
# audit entries, not transitions), and such a row is always LATER than the real
# one -- without the filter, prioritizing an order would backdate it to "just
# moved" and hide exactly the order somebody flagged as urgent.
# ``from_status IS NULL`` is kept on purpose: that is the creation row, which IS a
# real status entry. ``created_at`` covers any order with no usable row.
BACKFILL_STATUS_CHANGED_AT = """
    UPDATE orders o SET status_changed_at = COALESCE(
        (SELECT MAX(h.created_at) FROM order_status_history h
          WHERE h.order_id = o.id
            AND h.to_status = o.status
            AND (h.from_status IS NULL OR h.from_status <> h.to_status)),
        o.created_at)
"""

# When did the banding stop being blocked? The first banded piece cut.
#
# ``json_typeof(p.edges) <> 'null'`` and NOT ``p.edges IS NOT NULL``: ``edges`` is
# a plain JSON column and SQLAlchemy persists Python ``None`` into it as the JSON
# value ``null``, so the SQL NULL check is true for EVERY row and would date the
# clock off a piece that carries no banding at all. Same trap the bander's gate
# already hit in ``_is_banded()``.
BACKFILL_BANDING_READY_AT = """
    UPDATE orders o SET banding_ready_at = (
        SELECT MIN(p.cut_at) FROM order_placed_pieces p
         WHERE p.order_id = o.id
           AND json_typeof(p.edges) <> 'null'
           AND p.cut_at IS NOT NULL)
"""


def upgrade() -> None:
    op.add_column(
        'orders', sa.Column('status_changed_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('banding_ready_at', sa.DateTime(), nullable=True)
    )

    op.execute(BACKFILL_STATUS_CHANGED_AT)
    op.execute(BACKFILL_BANDING_READY_AT)


def downgrade() -> None:
    op.drop_column('orders', 'banding_ready_at')
    op.drop_column('orders', 'status_changed_at')
