"""board<->tapacanto coordination becomes a table of ours

Revision ID: 000000000010
Revises: 000000000009
Create Date: 2026-09-17

The coordination between a board and its edge bandings was a free-text ``family``
key inside each product's ``attributes`` bag, seeded from the vendor's
``marticulo.obs``. That made it unmanageable from this side for a mechanical
reason rather than a conceptual one: the sync's update does
``product.attributes = row.attributes`` -- a wholesale replacement of the bag --
so a family (or an alias, or ``grainDirection``) typed into the product form was
wiped on the next pass. The only stable place to configure it was the vendor's
own system.

So it moves to a table of ours (``product_families``) pointed at by a real
column (``products.family_id``), and ``alias`` moves out of the bag to
``products.alias``. Columns are immune to that overwrite by construction: the
sync only ever writes the columns it names. Same move as the workshop codes
(``000000000008``) and ``branches.warehouse_code`` (``000000000007``), for the
same reason.

**This migration deliberately does NOT touch the bag.** With it applied, the old
code still works (it reads ``attributes['family']``, which is still there) and
the new code works too (it reads the columns), so there is a real window to roll
the deploy back without rolling the database back. Stripping the two keys out of
the JSON is ``000000000011``, and it ships days later.

``normalized_name`` is a stored column rather than a functional unique index on
``lower(name)``: ``products.service.normalize_family`` casefolds, which is not
Postgres' ``lower()``, and two definitions of "the same family" would diverge the
day a name leaves ASCII. The rule stays in Python; the database enforces
uniqueness of what it is handed.

The three backfills live as module constants so the suite can run them against
real rows (``tests/test_product_family_backfill.py``) -- an ``op.execute`` string
is otherwise only ever exercised against an empty database, where every one of
them is a no-op.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000010'
down_revision: Union[str, None] = '000000000009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# One family row per distinct design, grouped case-insensitively.
#
# Three things are load-bearing here:
#
#   * ``json_typeof(attributes) = 'object'`` -- ``attributes`` is a plain ``JSON``
#     column (not ``JSONB``), and ``json ->> 'key'`` RAISES on a value that is not
#     an object rather than returning NULL. Same guard, same reason, as
#     ``000000000005``'s ``= 'array'``. Measured on the live catalog: 0 such rows,
#     which is exactly why the guard costs nothing.
#   * ``coalesce(..., '')`` -- the 76 boards with no family carry the key with a
#     JSON ``null`` value, so ``attributes->>'family'`` is SQL NULL, not ''. A
#     bare ``<> ''`` would let every one of them through as a family named NULL.
#   * ``min(...)`` as the display name -- the group key is the casefolded name, so
#     two spellings of one design would collide; ``min`` makes which one survives
#     deterministic instead of whatever the scan happened to read last. (Measured:
#     there are no such variants today. The point is that the migration cannot
#     produce a different catalog on a different machine.)
SEED_FAMILIES_FROM_ATTRIBUTES = """
INSERT INTO product_families (name, normalized_name, created_at, updated_at)
SELECT min(btrim(attributes->>'family')) AS name,
       lower(btrim(attributes->>'family')) AS normalized_name,
       now(),
       now()
  FROM products
 WHERE json_typeof(attributes) = 'object'
   AND btrim(coalesce(attributes->>'family', '')) <> ''
 GROUP BY lower(btrim(attributes->>'family'))
"""


# Point every product at the family its own bag named. The join is on the
# casefolded name, which is what ``normalize_family`` produces and what the
# seed above stored.
LINK_PRODUCTS_TO_FAMILIES = """
UPDATE products p
   SET family_id = f.id
  FROM product_families f
 WHERE json_typeof(p.attributes) = 'object'
   AND btrim(coalesce(p.attributes->>'family', '')) <> ''
   AND lower(btrim(p.attributes->>'family')) = f.normalized_name
"""


# The alias is copied VERBATIM -- no btrim, no nullif.
#
# That is not laziness: the alias is one of the three fields salted into
# ``optimizations.service._compute_hash`` (price + bandType + alias), so a value
# that changes by a single character changes the digest and every cached quote
# carrying that tapacanto recomputes. The point of this whole change is that
# nothing about the plan moves, so the string has to survive byte-identical.
# Pre-flight on the live catalog before writing this: 0 empty aliases, 0 with
# surrounding whitespace.
COPY_ALIAS_TO_COLUMN = """
UPDATE products
   SET alias = attributes->>'alias'
 WHERE type = 'edge_banding'
   AND json_typeof(attributes) = 'object'
   AND attributes->>'alias' IS NOT NULL
"""


def upgrade() -> None:
    op.create_table(
        'product_families',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('normalized_name', sa.String(length=64), nullable=False),
        sa.Column('description', sa.String(length=256), nullable=True),
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
            name=op.f('fk_product_families_created_by_users'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['updated_by'],
            ['users.id'],
            name=op.f('fk_product_families_updated_by_users'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_product_families')),
        sa.UniqueConstraint(
            'normalized_name', name=op.f('uq_product_families_normalized_name')
        ),
    )

    op.add_column('products', sa.Column('family_id', sa.Integer(), nullable=True))
    op.add_column('products', sa.Column('alias', sa.String(length=20), nullable=True))
    op.create_index(
        op.f('ix_products_family_id'), 'products', ['family_id'], unique=False
    )
    op.create_foreign_key(
        op.f('fk_products_family_id_product_families'),
        'products',
        'product_families',
        ['family_id'],
        ['id'],
        ondelete='SET NULL',
    )
    # Promoting the alias out of the bag opened it to every product type; it is
    # an edge-banding field and only ever was (``BoardAttributes`` never declared
    # one, and ``test_products.py`` pins that a board sending one has it dropped).
    op.create_check_constraint(
        'alias_only_on_edge_banding',
        'products',
        "alias IS NULL OR type = 'edge_banding'",
    )

    op.execute(SEED_FAMILIES_FROM_ATTRIBUTES)
    op.execute(LINK_PRODUCTS_TO_FAMILIES)
    op.execute(COPY_ALIAS_TO_COLUMN)


def downgrade() -> None:
    # Clean and lossless: the bag was never touched, so ``attributes`` still
    # carries every family and alias this migration copied out of it.
    op.drop_constraint(
        op.f('ck_products_alias_only_on_edge_banding'), 'products', type_='check'
    )
    op.drop_constraint(
        op.f('fk_products_family_id_product_families'), 'products', type_='foreignkey'
    )
    op.drop_index(op.f('ix_products_family_id'), table_name='products')
    op.drop_column('products', 'alias')
    op.drop_column('products', 'family_id')
    op.drop_table('product_families')
