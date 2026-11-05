"""the L of the edge notation is the longer side: the saved quotes keep their text

Revision ID: 000000000022
Revises: 000000000021
Create Date: 2026-11-04

The notation used to call ``L`` the sides along a piece's HEIGHT (its first
measure), whichever measure was longer, and the web turned what the seller
typed into sides by that reading: a ``1L`` became ``left``. It now calls ``L``
the pair along the LONGER measure, the shop's own reading
(``labels.long_sides``). The two only disagree on a piece whose width is the
longer measure, and there the saved side was the one the seller did NOT mean:
a 70×2050 strip typed ``2L`` was saved on its two 70 mm ends, billed 0.14 m of
tape instead of 4.1 m and drawn banded there, while the paper said ``2L`` and
the shop banded the long sides.

So every saved quote keeps the TEXT the seller typed and the shop read, and its
sides move to say it: on each row whose width is longer than its height, the
Canto's sides and the cantos especiales move to the other pair (``left``↔``top``,
``right``↔``bottom``). The metres, the price and the hard-edge cut of those rows
change to the ones the text always meant; their hash changes, so they are
re-optimized once on read. A square row, and every row whose height is the
longer measure, is untouched -- and so is a row with no banding at all.

Orders are NOT touched: a snapshot is immutable, and one frozen before the rule
carries no ``edge_notation`` mark, so it keeps printing exactly what the shop
read. The optimizer's drafts and the designer's projects store the web's form
state, which the API never reads; the web moves those when it opens one
(``OptimizerDraftPayload.version`` 2).

A swap of pairs is its own inverse, so the downgrade runs the same function.
That also means it is NOT idempotent: run once. The backfill lives as a module
function so ``tests/test_edge_notation_backfill.py`` replays it on real rows.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "000000000022"
down_revision: Union[str, None] = "000000000021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Each side to the one at the same place on the other pair.
_OTHER_PAIR = {"left": "top", "right": "bottom", "top": "left", "bottom": "right"}
# The orders the web sends them in: the Canto's sides (``EDGE_SIDES``) and the
# cantos especiales (``sortBySide``), so re-saving a moved row changes nothing.
_CANTO_ORDER = ("top", "bottom", "left", "right")
_SPECIAL_ORDER = ("left", "right", "top", "bottom")


def transpose_row(requirement: dict) -> bool:
    """Moves a wider-than-tall row's banding to the other pair, in place.

    Whether the row changed: never on a row whose height is at least its width,
    nor on one with no banding, nor on one banded alike on both pairs (``4L``
    on a single tape lands on the very same sides).
    """
    if not requirement.get("height", 0) < requirement.get("width", 0):
        return False
    before = json.dumps(requirement, sort_keys=True)
    canto = requirement.get("edge_banding")
    if canto and canto.get("sides"):
        canto["sides"] = sorted(
            (_OTHER_PAIR[s] for s in canto["sides"]), key=_CANTO_ORDER.index
        )
    for edge in requirement.get("special_edges") or []:
        edge["side"] = _OTHER_PAIR[edge["side"]]
    if requirement.get("special_edges"):
        requirement["special_edges"].sort(key=lambda e: _SPECIAL_ORDER.index(e["side"]))
    return json.dumps(requirement, sort_keys=True) != before


def backfill(bind) -> int:
    """Moves every saved quote's wider-than-tall rows; how many quotes it wrote."""
    written = 0
    rows = bind.execute(
        sa.text(
            "SELECT id, requirements FROM preorders "
            "WHERE json_typeof(requirements) = 'array' ORDER BY id"
        )
    ).all()
    for preorder_id, requirements in rows:
        moved = False
        for requirement in requirements:
            moved |= transpose_row(requirement)
        if moved:
            bind.execute(
                sa.text(
                    "UPDATE preorders SET requirements = CAST(:r AS json) "
                    "WHERE id = :id"
                ),
                {"r": json.dumps(requirements), "id": preorder_id},
            )
            written += 1
    return written


def upgrade() -> None:
    backfill(op.get_bind())


def downgrade() -> None:
    backfill(op.get_bind())
