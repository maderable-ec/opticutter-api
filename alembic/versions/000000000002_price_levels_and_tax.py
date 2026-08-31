"""Catalog price levels from SIFAC + explicit sales tax

Replaces the price-tier discount (a percentage this system invented) with the
three sale prices the vendor's inventory already publishes per article, and
makes the tax explicit instead of baked into every stored price.

Schema only. The catalog rows still hold TAX-INCLUDED prices after this runs:
every product comes from the sync, whose update pass overwrites ``price``
unconditionally, so ``POST /products/sync`` is a required deployment step right
after ``alembic upgrade`` — it is also what fills ``price_2``/``price_3``.
Between the two, the catalog is ~15% expensive and every level bills the same.

``orders``/``preorders`` are empty in production, so the server defaults here
exist only to keep the DDL valid on non-empty development databases.

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-08-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "000000000002"
down_revision: Union[str, None] = "000000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- products: the two reduced price levels ---------------------------------
    # Nullable on purpose: the vendor writes 0.000000 for a level nobody loaded
    # (11 articles have no level 2, 30 no level 3), and billing falls back to the
    # list price rather than quoting a board at $0.
    op.add_column("products", sa.Column("price_2", sa.Float(), nullable=True))
    op.add_column("products", sa.Column("price_3", sa.Float(), nullable=True))
    op.create_check_constraint(
        "price_2_non_negative", "products", "price_2 IS NULL OR price_2 >= 0"
    )
    op.create_check_constraint(
        "price_3_non_negative", "products", "price_3 IS NULL OR price_3 >= 0"
    )

    # --- settings: the tax rate replaces the tier list --------------------------
    op.add_column(
        "settings",
        sa.Column("tax_rate", sa.Float(), server_default="0.15", nullable=False),
    )
    op.drop_column("settings", "price_tiers")

    # --- preorders --------------------------------------------------------------
    op.add_column(
        "preorders",
        sa.Column("price_level", sa.Integer(), server_default="1", nullable=False),
    )
    op.drop_column("preorders", "price_tier_code")

    # --- orders -----------------------------------------------------------------
    op.drop_constraint("discount_amount_non_negative", "orders", type_="check")
    op.drop_constraint("discount_rate_ratio", "orders", type_="check")
    op.add_column(
        "orders",
        sa.Column("price_level", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "orders", sa.Column("tax_rate", sa.Float(), server_default="0", nullable=False)
    )
    op.add_column(
        "orders",
        sa.Column("tax_amount", sa.Float(), server_default="0", nullable=False),
    )
    op.drop_column("orders", "price_tier_code")
    op.drop_column("orders", "discount_amount")
    op.drop_column("orders", "discount_rate")
    op.create_check_constraint("tax_amount_non_negative", "orders", "tax_amount >= 0")
    op.create_check_constraint(
        "tax_rate_ratio", "orders", "tax_rate >= 0 AND tax_rate <= 1"
    )
    op.create_check_constraint(
        "price_level_in_range", "orders", "price_level BETWEEN 1 AND 3"
    )


def downgrade() -> None:
    op.drop_constraint("price_level_in_range", "orders", type_="check")
    op.drop_constraint("tax_rate_ratio", "orders", type_="check")
    op.drop_constraint("tax_amount_non_negative", "orders", type_="check")
    op.add_column(
        "orders",
        sa.Column(
            "price_tier_code",
            sa.VARCHAR(length=32),
            server_default="consumidor",
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column("discount_rate", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("discount_amount", sa.Float(), server_default="0", nullable=False),
    )
    op.drop_column("orders", "tax_amount")
    op.drop_column("orders", "tax_rate")
    op.drop_column("orders", "price_level")
    op.create_check_constraint(
        "discount_amount_non_negative", "orders", "discount_amount >= 0"
    )
    op.create_check_constraint(
        "discount_rate_ratio", "orders", "discount_rate >= 0 AND discount_rate <= 1"
    )

    op.add_column(
        "preorders",
        sa.Column(
            "price_tier_code",
            sa.VARCHAR(length=32),
            server_default="consumidor",
            nullable=False,
        ),
    )
    op.drop_column("preorders", "price_level")

    op.add_column(
        "settings",
        sa.Column(
            "price_tiers",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.drop_column("settings", "tax_rate")

    op.drop_constraint("price_3_non_negative", "products", type_="check")
    op.drop_constraint("price_2_non_negative", "products", type_="check")
    op.drop_column("products", "price_3")
    op.drop_column("products", "price_2")
