"""the cut rules' millimetres are settings

Revision ID: 000000000020
Revises: 000000000019
Create Date: 2026-10-27

How much the saw cuts short per side with a hard tape (``hard_edges``) and how
much larger it cuts every layer of an assembly (``assembly``) were fixed in the
code, 1 and 5 mm. They become cutting settings (``cut_rules``), edited by the
admin like the kerf.

Two integers on the singleton ``settings`` row, NOT NULL with server defaults
of 1 and 5 -- the values the code had fixed -- so the row that exists reads
exactly as before and no quote moves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '000000000020'
down_revision: Union[str, None] = '000000000019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'settings',
        sa.Column('hard_edge_cut_mm', sa.Integer(), server_default='1', nullable=False),
    )
    op.add_column(
        'settings',
        sa.Column(
            'assembly_allowance_mm', sa.Integer(), server_default='5', nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column('settings', 'assembly_allowance_mm')
    op.drop_column('settings', 'hard_edge_cut_mm')
