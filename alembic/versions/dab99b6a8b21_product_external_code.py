"""product external_code (catalog sync upsert key)

Revision ID: dab99b6a8b21
Revises: b3f1a9c40d21
Create Date: 2026-08-21 00:00:00.000000

External catalog sync (``products/catalog_sync.py``) upserts by a namespaced
key (``"{CATEGORIA}:{CODIGO}"``, e.g. ``"TABLEROS:1033"``) rather than by
``code``, because the vendor's numeric ``CODIGO`` can repeat across (and even
within) their export files. Nullable: only products created by the sync
populate it; manually created products leave it unset and are never touched
by a re-sync (including its deactivation pass, which only reconciles
products that already have an ``external_code``).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dab99b6a8b21'
down_revision: Union[str, None] = 'b3f1a9c40d21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'products',
        sa.Column('external_code', sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        op.f('uq_products_external_code'), 'products', ['external_code']
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('uq_products_external_code'), 'products', type_='unique'
    )
    op.drop_column('products', 'external_code')
