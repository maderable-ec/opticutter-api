"""drop the pre-order packing strategy

Revision ID: 000000000004
Revises: 000000000003
Create Date: 2026-09-15

The optimizer used to expose two packing heuristics and ``preorders.strategy``
remembered which one a quote was cut with, because a pre-order re-optimizes on
every read. The second heuristic (``longOffcuts``) is gone: its contract was
geometric -- concentrate the waste into one long reusable strip -- so the search
deliberately skipped the beam, the LNS, the CP-SAT endgame and the kerf repair
for it, and it could only ever bill MORE than the default.

The column is dropped rather than left behind because it is not inert data: it
feeds ``PreOrderService.build_request`` straight into an ``OptimizeRequest``, so
a surviving ``'longOffcuts'`` row would fail validation on every read of that
quote once the API enum is gone. Dropping it re-quotes those pre-orders at the
default heuristic on their next read, which is the point of the change.

Orders are NOT touched: their ``optimization_snapshot`` may still carry a
``"strategy"`` key inside the frozen JSON, but it is read as a plain dict and
never re-validated against an enum, so an already-issued order keeps rendering
exactly as it was billed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000004'
down_revision: Union[str, None] = '000000000003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('preorders', 'strategy')


def downgrade() -> None:
    # Restored with its old default, so a rollback leaves every row saying
    # ``default`` -- which is what every quote is cut with from now on anyway.
    op.add_column(
        'preorders',
        sa.Column(
            'strategy',
            sa.String(length=32),
            server_default='default',
            nullable=True,
        ),
    )
