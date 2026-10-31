"""the designer's projects, and the quote that remembers its project

Revision ID: 000000000021
Revises: 000000000020
Create Date: 2026-10-31

A new role, ``proyectista``, designs projects with the optimizer and hands them
to a seller to quote. The role itself needs no migration (``users.roles`` is a
string array). Both changes are additive:

- ``projects``: one row per project, owned by its designer (``owner_id``), with
  the optimizer's form state as an opaque ``payload`` and its status.
- ``preorders.project_id``: the project a quote was made from. Nullable; every
  existing quote reads NULL.

No data moves. The downgrade drops both, losing every project.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "000000000021"
down_revision: Union[str, None] = "000000000020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "projects"


def _user_fk(column: str, ondelete: Union[str, None] = "SET NULL"):
    return sa.ForeignKeyConstraint(
        [column],
        ["users.id"],
        name=op.f(f"fk_{_TABLE}_{column}_users"),
        ondelete=ondelete,
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("client_name", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(length=512), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ready_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        _user_fk("owner_id", ondelete=None),
        _user_fk("created_by"),
        _user_fk("updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index("ix_projects_owner_status", _TABLE, ["owner_id", "status"])
    op.create_index("ix_projects_status", _TABLE, ["status"])

    op.add_column("preorders", sa.Column("project_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_preorders_project_id_projects"),
        "preorders",
        _TABLE,
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_preorders_project_id", "preorders", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_preorders_project_id", table_name="preorders")
    op.drop_constraint(
        op.f("fk_preorders_project_id_projects"), "preorders", type_="foreignkey"
    )
    op.drop_column("preorders", "project_id")
    op.drop_index("ix_projects_status", table_name=_TABLE)
    op.drop_index("ix_projects_owner_status", table_name=_TABLE)
    op.drop_table(_TABLE)
