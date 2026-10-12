"""pauses with a reason, payments after the sale, and the shop's working hours

Revision ID: 000000000017
Revises: 000000000016
Create Date: 2026-10-12

Three things business asked for, all additive:

- ``order_activity_pauses`` + ``order_activities.paused_at`` / ``pause_reason``:
  an activity in progress can be paused with a reason (no boards, no edge
  banding, machine failure), so the stretch stays out of the worker's time.
  The activity's ``status`` does not gain a value: paused is ``in_progress``
  with the mirror columns set.
- ``order_payments``: money received after the sale, against the credit. The
  three amounts frozen on the order stay as the sale's terms, so nothing is
  backfilled and no order changes what it says it was sold for.
- ``settings.work_schedule``: the working hours the cut's time is read against.
  Nullable; a NULL reads as the default schedule.

No data moves. The downgrade drops the three, losing whatever was registered.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "000000000017"
down_revision: Union[str, None] = "000000000016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _audit_columns() -> list:
    return [
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
    ]


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        ["users.id"],
        name=op.f(f"fk_{table}_{column}_users"),
        ondelete="SET NULL",
    )


def upgrade() -> None:
    op.add_column(
        "order_activities", sa.Column("paused_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "order_activities",
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
    )

    op.create_table(
        "order_activity_pauses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("started_by", sa.Integer(), nullable=True),
        sa.Column("started_by_label", sa.String(length=128), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("ended_by", sa.Integer(), nullable=True),
        sa.Column("ended_by_label", sa.String(length=128), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["activity_id"],
            ["order_activities.id"],
            name=op.f("fk_order_activity_pauses_activity_id_order_activities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_activity_pauses_order_id_orders"),
            ondelete="CASCADE",
        ),
        _user_fk("order_activity_pauses", "started_by"),
        _user_fk("order_activity_pauses", "ended_by"),
        _user_fk("order_activity_pauses", "created_by"),
        _user_fk("order_activity_pauses", "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_activity_pauses")),
    )
    op.create_index(
        "ix_order_activity_pauses_activity_id",
        "order_activity_pauses",
        ["activity_id"],
    )
    op.create_index(
        "ix_order_activity_pauses_order_id", "order_activity_pauses", ["order_id"]
    )
    op.create_index(
        "ix_order_activity_pauses_started_at",
        "order_activity_pauses",
        ["started_at"],
    )
    # One pause in force per activity.
    op.create_index(
        "uq_order_activity_pauses_open",
        "order_activity_pauses",
        ["activity_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "order_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("received_by", sa.Integer(), nullable=True),
        sa.Column("received_by_label", sa.String(length=128), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("voided_by_label", sa.String(length=128), nullable=True),
        sa.Column("void_reason", sa.String(length=255), nullable=True),
        *_audit_columns(),
        sa.CheckConstraint(
            "amount > 0", name=op.f("ck_order_payments_amount_positive")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_payments_order_id_orders"),
            ondelete="CASCADE",
        ),
        _user_fk("order_payments", "received_by"),
        _user_fk("order_payments", "voided_by"),
        _user_fk("order_payments", "created_by"),
        _user_fk("order_payments", "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_payments")),
    )
    op.create_index("ix_order_payments_order_id", "order_payments", ["order_id"])
    op.create_index("ix_order_payments_paid_at", "order_payments", ["paid_at"])

    op.add_column("settings", sa.Column("work_schedule", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "work_schedule")
    op.drop_table("order_payments")
    op.drop_table("order_activity_pauses")
    op.drop_column("order_activities", "pause_reason")
    op.drop_column("order_activities", "paused_at")
