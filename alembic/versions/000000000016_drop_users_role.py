"""users.role goes: the roles are the list

Revision ID: 000000000016
Revises: 000000000015
Create Date: 2026-10-07

Phase two of ``000000000013``. That one added ``users.roles`` and kept
``users.role`` as a mirror of the primary role, so the release before it could
still read a valid user. Nothing has read the mirror since 013 shipped
(2026-09-23), and the web stopped sending and reading the single ``role`` the
same day, so the column and every legacy ``role`` of the contract go.

The downgrade brings the column back filled from ``roles[1]`` (Postgres arrays
start at 1): the primary role, which is exactly what the mirror held. The
restore lives as a module constant so the suite can replay it against real rows
(``tests/test_user_roles_backfill.py``).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000016'
down_revision: Union[str, None] = '000000000015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The mirror held the primary role: the first of the canonical list.
RESTORE_ROLE = """
UPDATE users
   SET role = roles[1]
 WHERE role IS NULL
"""


def upgrade() -> None:
    op.drop_column('users', 'role')


def downgrade() -> None:
    op.add_column('users', sa.Column('role', sa.String(length=32), nullable=True))
    op.execute(RESTORE_ROLE)
    op.alter_column('users', 'role', nullable=False)
