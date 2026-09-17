"""workshop codes on the cut list

Revision ID: 000000000008
Revises: 000000000007
Create Date: 2026-09-17

The shop's own work on a piece -- abisagrado, ensamble, ranurado -- is typed by
the seller on the cut list as a code the workshop already knows, and it is what
decides whether an order carries the ``additional`` activity. Until now that
activity came from the BILLED additional services, which are lines on the bill
and not work on the floor.

Six columns, the same three on both piece tables: ``order_pieces`` (the cut
list the order detail and the PDF show) and ``order_placed_pieces`` (the
instances the operator marks, which the additional work's floors count in SQL
-- which is why they are columns and not a JSON bag). Nullable, and NULL is the
only spelling of "no such work": ``Requirement`` turns a blank into ``None``
before anything is stored. Not backfilled: no order before this had codes.

One data change rides along. Every ``additional`` row that exists today was
created by a billed service, and none of those orders has a single piece with a
code -- so under the new floor (start on the first WORKED piece cut) a
``pending`` one could never start, and it would hold its order out of
``finished`` for good. ``DROP_UNWORKED_ADDITIONAL`` removes exactly those: still
``pending``, on an order that is still open. An ``in_progress`` row is left
alone because its close passes (zero worked pieces, zero missing), and a
``done`` one because it is the bander's history and analytics reads it.

The statement lives as a module constant so the suite runs it against real rows
(``tests/test_workshop_codes_migration.py``), for the same reason the backfills
of ``000000000003`` do.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000008'
down_revision: Union[str, None] = '000000000007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('order_pieces', 'order_placed_pieces')
_CODE_COLUMNS = ('hinging_code', 'assembly_code', 'grooving_code')

# The open statuses: the ones an ``additional`` row can still hold back.
DROP_UNWORKED_ADDITIONAL = """
DELETE FROM order_activities a
USING orders o
WHERE a.order_id = o.id
  AND a.type = 'additional'
  AND a.status = 'pending'
  AND o.status IN ('confirmed', 'queued', 'in_process')
"""

# The downgrade puts back what the old rule would have built for an open order:
# a pending row wherever the snapshot bills a service and the row is missing.
RESTORE_BILLED_ADDITIONAL = """
INSERT INTO order_activities (order_id, type, status, created_at, updated_at)
SELECT o.id, 'additional', 'pending', o.created_at, o.created_at
FROM orders o
WHERE o.status IN ('confirmed', 'queued', 'in_process')
  AND COALESCE(
      json_array_length(o.optimization_snapshot -> 'additional_services'), 0
  ) > 0
  AND NOT EXISTS (
      SELECT 1 FROM order_activities a
      WHERE a.order_id = o.id AND a.type = 'additional'
  )
"""


def upgrade() -> None:
    for table in _TABLES:
        for column in _CODE_COLUMNS:
            op.add_column(table, sa.Column(column, sa.String(length=32), nullable=True))
    op.execute(DROP_UNWORKED_ADDITIONAL)


def downgrade() -> None:
    op.execute(RESTORE_BILLED_ADDITIONAL)
    for table in _TABLES:
        for column in _CODE_COLUMNS:
            op.drop_column(table, column)
