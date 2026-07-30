"""preorder variant (alternative-solution seed)

Revision ID: b3f1a9c40d21
Revises: 8c2668619f23
Create Date: 2026-07-30 00:00:00.000000

``variant`` is the alternative-solution seed of the board-count search
("Generar otra alternativa"): like ``strategy`` it affects the recompute's
geometry and the optimization hash, so the pre-order persists it to reproduce
the chosen layout on every read and the order inherits it on confirmation.
``server_default = 0`` keeps every existing pre-order on the canonical
solution.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3f1a9c40d21'
down_revision: Union[str, None] = '8c2668619f23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'preorders',
        sa.Column('variant', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('preorders', 'variant')
