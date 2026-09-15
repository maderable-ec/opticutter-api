"""the material each cut-list piece is cut from

Revision ID: 000000000005
Revises: 000000000004
Create Date: 2026-09-15

``order_pieces`` froze only the resolved ``product_id``, which cannot name the
material in the two cases the order detail most needs it to: a client's offcut
or a manual measurement has no catalog product at all (``product_id`` is NULL),
and two pools of the SAME board -- one squared, one ``skipTrim`` -- share a
product while being two different materials. ``material_key`` is the identity
the rest of the optimization uses, and it was on the requirement all along
(``_dump_requirement`` puts ``material_key``, ``product_code`` and
``product_name`` on every one); ``create()`` simply dropped all three.

``order_boards`` already freezes exactly these columns, which is what lets the
cutting plan name the sheet it draws. This is the same move for the cut list.

The backfill lives as a module constant so the suite can run it against real
rows (``tests/test_order_piece_material_backfill.py``): an ``op.execute`` string
is otherwise only ever exercised on an empty database, where it is a no-op --
and this one pairs two lists by POSITION, which is the kind of thing that is
silent when it is wrong.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '000000000005'
down_revision: Union[str, None] = '000000000004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Recover each piece's material from the order's own frozen snapshot.
#
# The pairing is POSITIONAL because there is nothing else to pair on: the rows
# were built by one comprehension over ``payload["requirements"]``
# (``orders/service.py``), so piece *i* is requirement *i*, and a label is
# neither unique nor always present. ``ORDER BY id`` is the only stable reading
# of "position" once the rows are in the table -- they were inserted in that
# same order.
#
# Two guards, both load-bearing, because a positional pairing that is off by one
# writes the WRONG material rather than none:
#
#   * ``json_typeof(...) = 'array'`` -- ``optimization_snapshot`` is a plain
#     ``JSON`` column (not ``JSONB``), and ``json_array_elements`` raises on a
#     value that is not an array instead of returning no rows.
#   * the length equality -- if an order's piece count and its requirement count
#     disagree, the two lists are not the same list and the order is skipped
#     whole. Its pieces keep a NULL material, which the detail renders as "--";
#     that is the honest answer, a shifted one is not.
BACKFILL_PIECE_MATERIAL = """
WITH req AS (
    SELECT o.id AS order_id,
           r.ord AS pos,
           r.value->>'material_key' AS material_key,
           r.value->>'product_code' AS product_code,
           r.value->>'product_name' AS product_name
      FROM orders o
      CROSS JOIN LATERAL json_array_elements(
               o.optimization_snapshot->'requirements'
           ) WITH ORDINALITY AS r(value, ord)
     WHERE json_typeof(o.optimization_snapshot->'requirements') = 'array'
),
piece AS (
    SELECT p.id,
           p.order_id,
           row_number() OVER (PARTITION BY p.order_id ORDER BY p.id) AS pos
      FROM order_pieces p
      JOIN orders o ON o.id = p.order_id
     WHERE json_typeof(o.optimization_snapshot->'requirements') = 'array'
       AND json_array_length(o.optimization_snapshot->'requirements') = (
               SELECT count(*) FROM order_pieces q WHERE q.order_id = p.order_id
           )
)
UPDATE order_pieces t
   SET material_key = req.material_key,
       product_code = req.product_code,
       product_name = req.product_name
  FROM piece, req
 WHERE t.id = piece.id
   AND req.order_id = piece.order_id
   AND req.pos = piece.pos
   AND req.material_key IS NOT NULL
"""


def upgrade() -> None:
    op.add_column(
        'order_pieces', sa.Column('material_key', sa.String(length=64), nullable=True)
    )
    op.add_column(
        'order_pieces', sa.Column('product_code', sa.String(length=64), nullable=True)
    )
    op.add_column(
        'order_pieces', sa.Column('product_name', sa.String(length=128), nullable=True)
    )

    op.execute(BACKFILL_PIECE_MATERIAL)


def downgrade() -> None:
    op.drop_column('order_pieces', 'product_name')
    op.drop_column('order_pieces', 'product_code')
    op.drop_column('order_pieces', 'material_key')
