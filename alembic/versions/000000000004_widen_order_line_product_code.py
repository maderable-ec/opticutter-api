"""Widen order_lines.product_code to 64 chars

A billing line for a non-catalog material (an offcut, a manual measurement) has
no product, so it is identified by the material's own ``key`` from the
optimization payload — and that key is allowed up to 64 characters while this
column was 32. Creating an order over such a material with a long key failed at
commit with ``value too long for type character varying(32)``. Its sibling
``order_boards.product_code`` was already 64; this aligns them.

Widening only, so nothing existing can be truncated. The downgrade truncates
because it has to.

Revision ID: 000000000004
Revises: 000000000003
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000004"
down_revision: Union[str, None] = "000000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "order_lines",
        "product_code",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE order_lines SET product_code = LEFT(product_code, 32)")
    op.alter_column(
        "order_lines",
        "product_code",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
