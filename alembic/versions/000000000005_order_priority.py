"""Priority attention flag on orders

The workshop board hands orders out strictly in arrival order, and that is the
right default -- but a client who needs their cut today has no way through it.
Business asked to be able to mark an order for priority attention: the shop floor
sees it highlighted and takes it first. FIFO stays the rule among orders of equal
priority.

Additive and non-commercial: the flag touches neither pricing nor the status
machine, only the order the board lists. ``server_default false`` is what every
existing order means -- none of them was ever prioritized -- so there is no
backfill and the column can be NOT NULL from the start.

Revision ID: 000000000005
Revises: 000000000004
Create Date: 2026-09-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000005"
down_revision: Union[str, None] = "000000000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "is_priority",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "is_priority")
