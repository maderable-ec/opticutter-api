"""an order belongs to whoever raised its quote

Revision ID: 000000000015
Revises: 000000000014
Create Date: 2026-10-06

Since ``POST /orders`` went away, every order is born from the client's click on
the review link, whose actor is no user: ``orders.created_by`` came out NULL on
every order, and the sales per seller of the statistics read exactly that
column. ``PreOrderReviewService.confirm`` now passes the quote's owner; this
backfills the orders already there from the OLDEST quote pointing at each one
(the one that minted it -- ``OrderModel.preorder``).

Data only, and additive: nothing reads a NULL here as meaningful, so the
previous release reads the filled column the same. Only NULLs are written. The
downgrade does nothing -- there is no telling a backfilled owner from a fresh
one, and blanking them would only bring the bug back.

The backfill lives as a module constant so the suite can replay it against real
rows (``tests/test_order_created_by_backfill.py``).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000015'
down_revision: Union[str, None] = '000000000014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKFILL_ORDER_CREATED_BY = """
UPDATE orders
   SET created_by = quote.created_by
  FROM (
        SELECT DISTINCT ON (order_id) order_id, created_by
          FROM preorders
         WHERE order_id IS NOT NULL
         ORDER BY order_id, id
       ) AS quote
 WHERE orders.id = quote.order_id
   AND orders.created_by IS NULL
   AND quote.created_by IS NOT NULL
"""


def upgrade() -> None:
    op.execute(BACKFILL_ORDER_CREATED_BY)


def downgrade() -> None:
    pass
