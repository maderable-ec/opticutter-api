"""order activities

Revision ID: 000000000003
Revises: 000000000002
Create Date: 2026-09-10

``in_process`` replaces ``cutting`` + ``cut``, and the work that used to BE those
two statuses becomes rows in ``order_activities`` -- one per applicable activity
(cut, banding, additional), each with its own status, clocks and actor. The
banding's eight columns on ``orders`` move into that table and disappear.

Three data migrations ride along, in this order and for these reasons:

1. The activity rows are backfilled while the OLD vocabulary is still in place:
   the cut's timestamps can only be recovered from history rows that say
   ``cutting``/``cut``, and its status from an ``orders.status`` that still says
   them too.
2. ``orders.status`` is then rewritten: ``cutting``/``cut`` → ``in_process``,
   ``completed`` → ``finished``, ``despachado`` → ``dispatched`` (the last one
   was the only Spanish value in an otherwise English enum, and its member name
   never matched it; rewriting statuses anyway is the moment to fix that).
3. ``order_status_history`` gets the same rename for ``completed`` and
   ``despachado`` but KEEPS ``cutting``/``cut``: the history is the log of what
   happened, in the vocabulary of the time, and collapsing that pair would turn
   a real transition into a ``from == to`` row -- which is the shape
   ``set_priority``/``change_branch`` use for things that are NOT transitions,
   and which the clock backfills specifically filter out.

The backfills live as module constants so the suite can run them against real
rows (``tests/test_order_activities_backfill.py``): an ``op.execute`` string is
otherwise only ever exercised on an empty database, where all of them are no-ops.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000003'
down_revision: Union[str, None] = '000000000002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# One cut activity per order. Its status is read off the order's own (old)
# status; its clocks off the two history rows that used to mark the start and
# the end of the cut. ``MIN`` and not ``MAX`` on purpose: with an admin rollback
# (``cutting -> queued -> cutting``) the honest start is the first time the shop
# took the order, which is what the operator's hours were always meant to count.
# ``ready_at`` is ``queued_at``: reaching the queue (being paid) is the moment
# the cut stopped being blocked.
BACKFILL_CUTTING_ACTIVITY = """
INSERT INTO order_activities (
    order_id, type, status, ready_at,
    started_at, started_by, started_by_label,
    finished_at, finished_by, finished_by_label,
    created_at, updated_at
)
SELECT
    o.id,
    'cutting',
    CASE
        WHEN o.status IN ('cut', 'completed', 'despachado') THEN 'done'
        WHEN o.status = 'cutting' THEN 'in_progress'
        ELSE 'pending'
    END,
    o.queued_at,
    started.at,
    CASE WHEN started.at IS NOT NULL THEN o.assigned_to_id END,
    CASE WHEN started.at IS NOT NULL THEN o.assigned_to_label END,
    finished.at,
    CASE WHEN finished.at IS NOT NULL THEN o.assigned_to_id END,
    CASE WHEN finished.at IS NOT NULL THEN o.assigned_to_label END,
    o.created_at,
    o.created_at
FROM orders o
LEFT JOIN (
    SELECT order_id, MIN(created_at) AS at
    FROM order_status_history WHERE to_status = 'cutting' GROUP BY order_id
) started ON started.order_id = o.id
LEFT JOIN (
    SELECT order_id, MIN(created_at) AS at
    FROM order_status_history WHERE to_status = 'cut' GROUP BY order_id
) finished ON finished.order_id = o.id
"""

# The banding track, verbatim from the columns it is leaving. ``not_applicable``
# gets NO ROW: that is exactly what the value meant, and the absence is what the
# new model uses to say an activity does not apply.
BACKFILL_BANDING_ACTIVITY = """
INSERT INTO order_activities (
    order_id, type, status, ready_at,
    started_at, started_by, started_by_label,
    finished_at, finished_by, finished_by_label,
    created_at, updated_at
)
SELECT
    o.id, 'banding', o.banding_status, o.banding_ready_at,
    o.banding_started_at, o.banding_started_by, o.banding_started_by_label,
    o.banding_finished_at, o.banding_finished_by, o.banding_finished_by_label,
    o.created_at, o.created_at
FROM orders o
WHERE o.banding_status <> 'not_applicable'
"""

# The additional work is new: nothing recorded it, so there are no timestamps to
# recover. A CLOSED order gets a ``done`` row -- the work did happen, under the
# old rules, and a ``pending`` row would leave finished orders looking unfinished
# forever. An OPEN one gets ``pending``, which is the new behavior taking effect:
# the bander has to register it before the order can close.
#
# ``->`` on a missing key yields SQL NULL and ``json_array_length`` is strict, so
# the COALESCE is what covers snapshots written before additional services
# existed.
BACKFILL_ADDITIONAL_ACTIVITY = """
INSERT INTO order_activities (
    order_id, type, status, created_at, updated_at
)
SELECT
    o.id,
    'additional',
    CASE
        WHEN o.status IN ('completed', 'despachado') THEN 'done'
        ELSE 'pending'
    END,
    o.created_at,
    o.created_at
FROM orders o
WHERE COALESCE(
    json_array_length(o.optimization_snapshot -> 'additional_services'), 0
) > 0
"""

# ``cutting``/``cut`` collapse into one state; the other two are renames.
REWRITE_ORDER_STATUS = """
UPDATE orders SET status = CASE
    WHEN status IN ('cutting', 'cut') THEN 'in_process'
    WHEN status = 'completed' THEN 'finished'
    WHEN status = 'despachado' THEN 'dispatched'
    ELSE status
END
WHERE status IN ('cutting', 'cut', 'completed', 'despachado')
"""

# The history keeps ``cutting``/``cut`` (see the module docstring); only the two
# renamed values are rewritten, on both columns.
REWRITE_HISTORY_STATUS = """
UPDATE order_status_history SET
    from_status = CASE
        WHEN from_status = 'completed' THEN 'finished'
        WHEN from_status = 'despachado' THEN 'dispatched'
        ELSE from_status
    END,
    to_status = CASE
        WHEN to_status = 'completed' THEN 'finished'
        WHEN to_status = 'despachado' THEN 'dispatched'
        ELSE to_status
    END
WHERE from_status IN ('completed', 'despachado')
   OR to_status IN ('completed', 'despachado')
"""

# --- downgrade ---------------------------------------------------------------
# The banding columns can be refilled from their rows; the cut's and the
# additional work's cannot (there was nowhere to put them), which is why the
# reverse of step 1 is simply dropping the table.
RESTORE_BANDING_COLUMNS = """
UPDATE orders o SET
    banding_status = a.status,
    banding_ready_at = a.ready_at,
    banding_started_at = a.started_at,
    banding_started_by = a.started_by,
    banding_started_by_label = a.started_by_label,
    banding_finished_at = a.finished_at,
    banding_finished_by = a.finished_by,
    banding_finished_by_label = a.finished_by_label
FROM order_activities a
WHERE a.order_id = o.id AND a.type = 'banding'
"""

# ``in_process`` cannot tell which of the two states it came from, so the cut
# activity answers it: done → the order was ``cut``, otherwise ``cutting``.
REVERT_ORDER_STATUS = """
UPDATE orders o SET status = CASE
    WHEN o.status = 'in_process' THEN (
        CASE WHEN EXISTS (
            SELECT 1 FROM order_activities a
            WHERE a.order_id = o.id
              AND a.type = 'cutting'
              AND a.status = 'done'
        ) THEN 'cut' ELSE 'cutting' END
    )
    WHEN o.status = 'finished' THEN 'completed'
    WHEN o.status = 'dispatched' THEN 'despachado'
    ELSE o.status
END
WHERE o.status IN ('in_process', 'finished', 'dispatched')
"""

REVERT_HISTORY_STATUS = """
UPDATE order_status_history SET
    from_status = CASE
        WHEN from_status = 'finished' THEN 'completed'
        WHEN from_status = 'dispatched' THEN 'despachado'
        ELSE from_status
    END,
    to_status = CASE
        WHEN to_status = 'finished' THEN 'completed'
        WHEN to_status = 'dispatched' THEN 'despachado'
        ELSE to_status
    END
WHERE from_status IN ('finished', 'dispatched')
   OR to_status IN ('finished', 'dispatched')
"""

_BANDING_COLUMNS = (
    "banding_status",
    "banding_ready_at",
    "banding_started_at",
    "banding_started_by",
    "banding_started_by_label",
    "banding_finished_at",
    "banding_finished_by",
    "banding_finished_by_label",
)


def upgrade() -> None:
    op.create_table(
        'order_activities',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column(
            'status',
            sa.String(length=16),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('ready_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('started_by', sa.Integer(), nullable=True),
        sa.Column('started_by_label', sa.String(length=128), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('finished_by', sa.Integer(), nullable=True),
        sa.Column('finished_by_label', sa.String(length=128), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['created_by'],
            ['users.id'],
            name=op.f('fk_order_activities_created_by_users'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['updated_by'],
            ['users.id'],
            name=op.f('fk_order_activities_updated_by_users'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['finished_by'],
            ['users.id'],
            name=op.f('fk_order_activities_finished_by_users'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['order_id'],
            ['orders.id'],
            name=op.f('fk_order_activities_order_id_orders'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['started_by'],
            ['users.id'],
            name=op.f('fk_order_activities_started_by_users'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_order_activities')),
        sa.UniqueConstraint(
            'order_id', 'type', name='uq_order_activities_order_type'
        ),
    )
    op.create_index(
        'ix_order_activities_type_status',
        'order_activities',
        ['type', 'status'],
        unique=False,
    )

    # 1) Rows first, while the old vocabulary is still readable.
    op.execute(BACKFILL_CUTTING_ACTIVITY)
    op.execute(BACKFILL_BANDING_ACTIVITY)
    op.execute(BACKFILL_ADDITIONAL_ACTIVITY)

    # 2) The banding columns have moved.
    for column in _BANDING_COLUMNS:
        op.drop_column('orders', column)

    # 3) The vocabulary itself.
    op.execute(REWRITE_ORDER_STATUS)
    op.execute(REWRITE_HISTORY_STATUS)


def downgrade() -> None:
    op.add_column(
        'orders',
        sa.Column(
            'banding_status',
            sa.String(length=16),
            server_default='not_applicable',
            nullable=False,
        ),
    )
    op.add_column('orders', sa.Column('banding_ready_at', sa.DateTime(), nullable=True))
    op.add_column(
        'orders', sa.Column('banding_started_at', sa.DateTime(), nullable=True)
    )
    op.add_column('orders', sa.Column('banding_started_by', sa.Integer(), nullable=True))
    op.add_column(
        'orders', sa.Column('banding_started_by_label', sa.String(length=128), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('banding_finished_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('banding_finished_by', sa.Integer(), nullable=True)
    )
    op.add_column(
        'orders',
        sa.Column('banding_finished_by_label', sa.String(length=128), nullable=True),
    )
    op.create_foreign_key(
        op.f('fk_orders_banding_started_by_users'),
        'orders',
        'users',
        ['banding_started_by'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_orders_banding_finished_by_users'),
        'orders',
        'users',
        ['banding_finished_by'],
        ['id'],
        ondelete='SET NULL',
    )

    op.execute(RESTORE_BANDING_COLUMNS)
    op.execute(REVERT_ORDER_STATUS)
    op.execute(REVERT_HISTORY_STATUS)

    op.drop_index('ix_order_activities_type_status', table_name='order_activities')
    op.drop_table('order_activities')
