"""family and alias leave the attributes bag

Revision ID: 000000000011
Revises: 000000000010
Create Date: 2026-09-17

``000000000010`` copied ``family`` and ``alias`` out of the JSON bag into
``products.family_id`` and ``products.alias`` and deliberately left the originals
in place, so that the old code kept working and the deploy could be rolled back.
This removes them, which is what makes the columns the single source of truth.

Leaving them would not be harmless. The sync's update rewrites the whole bag on
every pass, so the stale copy would keep being refreshed from the vendor's
``obs`` -- the exact value this change exists to stop obeying -- and
``ProductResponse.attributes`` is an opaque ``dict``, so a client would see
``attributes.family`` and ``family`` side by side and some of them would read the
wrong one.

**Deploy this one late**, once the new code has been running for a while. From
here on the old frontend renders the family blank; that is cosmetic and loses no
data, but it is one-way in practice.

What stays in the bag is exactly what the VENDOR owns: dimensions, thickness,
subtype, ``bandType`` (inferred from the tape's own thickness), ``color`` and
``length``. ``grainDirection`` stays too and stays exposed to the overwrite --
a known gap, deliberately not closed here: nothing reads it and it is empty
across the whole catalog. If it ever matters, the fix is this same move.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000011'
down_revision: Union[str, None] = '000000000010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Drop the two keys from every bag that has either.
#
# ``attributes`` is ``JSON`` and not ``JSONB``, which decides the whole shape of
# this statement: the key-removal operator ``-`` and the key-existence operator
# ``?`` are **jsonb-only**. Hence the explicit round trip through ``jsonb``, and
# hence a ``WHERE`` written with ``->>`` instead of the ``?`` it would otherwise
# want. (``?`` would also collide with psycopg2's own parameter handling in a
# literal statement, so this is two reasons to avoid it.)
#
# ``json_typeof(...) = 'object'`` guards the ``->>``, which RAISES on a non-object
# rather than returning NULL -- the same guard ``000000000005`` documents.
#
# The jsonb round trip reorders keys and normalizes number formatting. Verified
# harmless: nothing depends on the bag's byte order. ``_compute_hash``
# re-serializes with ``sort_keys=True``, and every reader accesses by key and
# casts. The alternative (rebuilding the object with ``json_object_agg`` over
# ``json_each``) preserves order but needs a ``COALESCE`` for the empty-object
# case and buys nothing.
STRIP_FAMILY_AND_ALIAS = """
UPDATE products
   SET attributes = ((attributes::jsonb - 'family') - 'alias')::json
 WHERE json_typeof(attributes) = 'object'
   AND (attributes->>'family' IS NOT NULL OR attributes->>'alias' IS NOT NULL)
"""


# The downgrade rebuilds the bag from the columns, so a rollback lands on data
# the pre-010 code can read. The family comes back as its display ``name`` (what
# the bag held), not the normalized key.
RESTORE_FAMILY_AND_ALIAS = """
UPDATE products p
   SET attributes = (
           p.attributes::jsonb
           || jsonb_strip_nulls(
                  jsonb_build_object(
                      'family', to_jsonb(f.name),
                      'alias', to_jsonb(p.alias)
                  )
              )
       )::json
  FROM product_families f
 WHERE json_typeof(p.attributes) = 'object'
   AND p.family_id = f.id
"""


# A tapacanto can carry an alias while belonging to no family, which the join
# above would skip. Rare, but "rare" is not "never" once humans edit this.
RESTORE_ORPHAN_ALIAS = """
UPDATE products
   SET attributes = (
           attributes::jsonb || jsonb_build_object('alias', to_jsonb(alias))
       )::json
 WHERE json_typeof(attributes) = 'object'
   AND alias IS NOT NULL
   AND family_id IS NULL
"""


def upgrade() -> None:
    op.execute(STRIP_FAMILY_AND_ALIAS)


def downgrade() -> None:
    op.execute(RESTORE_FAMILY_AND_ALIAS)
    op.execute(RESTORE_ORPHAN_ALIAS)
