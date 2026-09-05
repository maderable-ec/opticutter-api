"""Freeze when an order actually entered the production queue

The workshop board listed orders by creation, but an order reaches the shop when
it is PAID, not when it is quoted: ``confirmed -> queued`` is gated on registering
the payment method. A quote raised on Monday and paid on Friday therefore reaches
production after one raised on Wednesday and paid on Thursday — and ordering by
creation handed the first slot to whoever asked first instead of whoever paid
first. ``queued_at`` is that arrival time, and the board's FIFO now reads it.

Backfilled from the order's own history, which already records every transition:
the EARLIEST row into ``queued`` is the moment it joined the line. Orders whose
history predates that record fall back to ``created_at`` (the old behavior, so
nothing moves for them). Orders that never entered the queue keep NULL —
``confirmed`` hasn't been paid yet, and ``cancelled`` is only ever reached from
``confirmed``, so neither was ever in the shop.

Revision ID: 000000000006
Revises: 000000000005
Create Date: 2026-09-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000006"
down_revision: Union[str, None] = "000000000005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("queued_at", sa.DateTime(), nullable=True))
    # MIN(): an order rolled back cutting -> queued has several rows into ``queued``,
    # and the one that counts is the first — the rollback must not re-date its place.
    op.execute(
        """
        UPDATE orders o
           SET queued_at = COALESCE(
                 (SELECT MIN(h.created_at)
                    FROM order_status_history h
                   WHERE h.order_id = o.id
                     AND h.to_status = 'queued'),
                 o.created_at)
         WHERE o.status NOT IN ('confirmed', 'cancelled')
        """
    )


def downgrade() -> None:
    op.drop_column("orders", "queued_at")
