"""a user holds a list of roles

Revision ID: 000000000013
Revises: 000000000012
Create Date: 2026-09-23

A bander learning to cut has to register the cut without giving up the banding:
``operador`` + ``canteador`` on one user. ``users.role`` held exactly one, so
this adds ``users.roles`` (a Postgres array, canonical order, never empty) and
the permissions become the union of its roles. Which roles may combine is a rule
of ``UserService``, not of the table, so it can be relaxed without a migration.

ADDITIVE ON PURPOSE. ``users.role`` stays, as a mirror of the primary role
(``roles[0]``) that the model keeps in step: the previous release reads that
column, and rolling the api back is a retag with no downgrade. Migration 014
drops it once this release has settled.

The backfill lives as a module constant so the suite can replay it against real
rows (``tests/test_user_roles_backfill.py``).

The downgrade drops ``roles``; the mirror is already the primary role, so the
previous release reads a valid user -- minus any secondary role, which is lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '000000000013'
down_revision: Union[str, None] = '000000000012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every existing user held one role; it becomes a one-element list.
BACKFILL_ROLES = """
UPDATE users
   SET roles = ARRAY[role]::varchar(32)[]
 WHERE roles IS NULL
"""


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('roles', postgresql.ARRAY(sa.String(length=32)), nullable=True),
    )
    op.execute(BACKFILL_ROLES)
    op.alter_column('users', 'roles', nullable=False)
    op.create_check_constraint('roles_not_empty', 'users', 'cardinality(roles) >= 1')


def downgrade() -> None:
    op.drop_constraint(op.f('ck_users_roles_not_empty'), 'users', type_='check')
    op.drop_column('users', 'roles')
