"""the shop's consumables: blade changes and glue loads

Revision ID: 000000000018
Revises: 000000000017
Create Date: 2026-10-12

Business wants to know how many saw blades, scoring blades and glue the shop
goes through, and what each one gives. Both additive:

- ``consumable_changes``: one row per change (a blade mounted, the glue pot
  loaded), per branch. The metres a blade cut are not stored: they are read
  off the production between two changes.
- ``settings.glue_load_kg``: the standard glue load, what the shop floor's
  control starts at. Nullable; a NULL reads as ``config.GLUE_LOAD_KG``.

No data moves. The downgrade drops both, losing whatever was registered.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "000000000018"
down_revision: Union[str, None] = "000000000017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "consumable_changes"


def _user_fk(column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        ["users.id"],
        name=op.f(f"fk_{_TABLE}_{column}_users"),
        ondelete="SET NULL",
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("changed_by_label", sa.String(length=128), nullable=True),
        sa.Column("condition", sa.String(length=16), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("voided_by_label", sa.String(length=128), nullable=True),
        sa.Column("void_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name=op.f(f"ck_{_TABLE}_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{_TABLE}_branch_id_branches"),
        ),
        _user_fk("changed_by"),
        _user_fk("voided_by"),
        _user_fk("created_by"),
        _user_fk("updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(
        "ix_consumable_changes_branch_type_at",
        _TABLE,
        ["branch_id", "type", "changed_at"],
    )

    op.add_column("settings", sa.Column("glue_load_kg", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "glue_load_kg")
    op.drop_table(_TABLE)
