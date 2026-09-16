"""index the quote's back-reference to its order

Revision ID: 000000000006
Revises: 000000000005
Create Date: 2026-09-16

``preorders.order_id`` has been a foreign key since the consolidated schema but
was never indexed: the only indexes on the table are the composites
``(branch_id, status)`` and ``(client_id, status)``, and Postgres does not index
a foreign key on its own. That was fine while the link was only ever read
FORWARDS -- from a quote you already had in hand.

The order now reads it BACKWARDS (``OrderModel.preorders``, a view-only
relationship over this very column), so the orders listing issues one
``WHERE order_id IN (...)`` per page and the order detail one per row. Without
this index both are sequential scans of the whole quote table, on the busiest
screen the office has.

No column and no data move: the relationship needs no storage of its own, which
is the whole reason the back-reference costs one index instead of a migration
that adds ``orders.preorder_id`` and then has to backfill it.

Deliberately NOT unique. The link is N:1, not 1:1 -- ``OrderService.create``
dedupes, so two distinct quotes of the same client, branch, hash and totals
resolve to ONE order and both legitimately write its id.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000006'
down_revision: Union[str, None] = '000000000005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_preorders_order_id', 'preorders', ['order_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_preorders_order_id', table_name='preorders')
