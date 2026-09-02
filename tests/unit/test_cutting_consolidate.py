"""Unit tests for the cut-tree re-derivation (``src/cutting/consolidate.py``).

Pure geometry, no DB. The contract has two halves and both are load-bearing:

* it must **consolidate** — the pre-order 4 shape is the case that motivated it;
* it must **never cost anything** — same placements, same board, and never a
  worse set of leftovers than the packer's own tree.

The second half is what makes the pass safe to run unconditionally, so most of
what follows is about that rather than about the headline win.
"""

import random

import pytest

from src.cutting import (
    CuttingLayout,
    CuttingParameters,
    Material,
    MultiSheetGuillotineOptimizer,
    PackingStrategy,
    Piece,
    Rectangle,
    consolidate_layout,
)
from src.cutting.consolidate import _score

# The shop's real setup: 4mm blade, 10mm trimmed off every edge.
PARAMS = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)
SHEET = Material(id="mdp", width=2070, height=2800, thickness=15, cost_per_unit=79.0)


def _pack(pieces, params=PARAMS, material=SHEET, strategy=None):
    optimizer = MultiSheetGuillotineOptimizer(
        material,
        params,
        strategy=strategy or PackingStrategy.MAX_EFFICIENCY,
    )
    layouts, unplaced = optimizer.optimize(pieces)
    assert not unplaced
    return layouts


def _dims(layout):
    return sorted((r.width, r.height) for r in layout.remainders)


def _assert_leftovers_are_sane(before, after, params=PARAMS):
    """Placements untouched; leftovers real, disjoint, and clear of the pieces."""
    assert [
        (p.piece.id, p.x, p.y, p.width, p.height, p.rotated)
        for p in after.placed_pieces
    ] == [
        (p.piece.id, p.x, p.y, p.width, p.height, p.rotated)
        for p in before.placed_pieces
    ]
    assert after.material is before.material
    assert after.sheet_number == before.sheet_number

    x0, y0 = params.left_trim, params.bottom_trim
    x1 = after.material.width - params.right_trim
    y1 = after.material.height - params.top_trim
    rects = after.remainders
    for rect in rects:
        assert rect.width > 0 and rect.height > 0
        assert rect.x >= x0 - 1e-6 and rect.y >= y0 - 1e-6
        assert rect.x + rect.width <= x1 + 1e-6
        assert rect.y + rect.height <= y1 + 1e-6
        for piece in after.placed_pieces:
            overlaps = (
                rect.x < piece.x + piece.width - 1e-6
                and piece.x < rect.x + rect.width - 1e-6
                and rect.y < piece.y + piece.height - 1e-6
                and piece.y < rect.y + rect.height - 1e-6
            )
            assert not overlaps, f"leftover {rect} overlaps piece {piece.piece.id}"
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            overlaps = (
                a.x < b.x + b.width - 1e-6
                and b.x < a.x + a.width - 1e-6
                and a.y < b.y + b.height - 1e-6
                and b.y < a.y + a.height - 1e-6
            )
            assert not overlaps, f"leftovers overlap: {a} vs {b}"


def _random_layouts(seed, count=6):
    """Furniture-shaped boards: a few big panels plus a tail of repeated parts."""
    rng = random.Random(seed)
    pieces = []
    for i in range(count):
        width = float(int(rng.uniform(120, 700) / 5) * 5)
        height = float(int(rng.uniform(150, 2400) / 5) * 5)
        pieces.append(
            Piece(
                id=f"p{i}",
                width=width,
                height=height,
                quantity=rng.randint(1, 6),
                can_rotate=rng.random() < 0.5,
            )
        )
    return _pack(pieces)


# --- the case this exists for ------------------------------------------------


def test_six_strips_leave_two_offcuts_instead_of_seven():
    """Pre-order 4: 6x 320x2500, no rotation, one sheet.

    The packer rips six full-height columns and crops each to 2500, leaving five
    identical 320x276 scraps. The same placements admit one crosscut plus six
    rips: a 2050x276 band and a 106x2500 strip.
    """
    pieces = [
        Piece(id=f"p{i}", width=320, height=2500, quantity=1, can_rotate=False)
        for i in range(6)
    ]
    (packed,) = _pack(pieces)
    assert len(packed.remainders) == 7

    consolidated = consolidate_layout(packed, PARAMS)

    assert _dims(consolidated) == [(106.0, 2500.0), (2050.0, 276.0)]
    _assert_leftovers_are_sane(packed, consolidated)


def test_consolidating_also_recovers_the_kerf_it_no_longer_burns():
    """Fewer cuts through the waste means slightly MORE offcut area, not less.

    The packer's tree rips full height through the top band; the consolidated one
    stops at the pieces, so the band comes back whole.
    """
    pieces = [
        Piece(id=f"p{i}", width=320, height=2500, quantity=1, can_rotate=False)
        for i in range(6)
    ]
    (packed,) = _pack(pieces)
    consolidated = consolidate_layout(packed, PARAMS)

    assert sum(r.area for r in consolidated.remainders) > sum(
        r.area for r in packed.remainders
    )
    assert consolidated.cut_length < packed.cut_length


# --- the guarantees ----------------------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_never_scores_worse_than_the_packers_own_tree(seed):
    """The packer's tree competes under the same score and wins ties.

    This is what makes the pass safe to run on every board unconditionally: the
    worst case is that nothing changes.
    """
    for packed in _random_layouts(seed):
        consolidated = consolidate_layout(packed, PARAMS)
        assert _score(
            consolidated.remainders, consolidated.cut_length, 150.0
        ) <= _score(packed.remainders, packed.cut_length, 150.0)


@pytest.mark.parametrize("seed", range(12))
def test_pieces_and_leftovers_stay_physically_valid(seed):
    for packed in _random_layouts(seed):
        _assert_leftovers_are_sane(packed, consolidate_layout(packed, PARAMS))


@pytest.mark.parametrize("seed", range(12))
def test_never_forfeits_usable_offcut_area(seed):
    """The first key of the objective, pinned.

    Scoring on rectangle size alone grows the biggest leftover but shaves the
    second-biggest into slivers; ranking usable area first is what stops that,
    and it is the reason the pass is a net gain rather than a trade.
    """
    for packed in _random_layouts(seed):
        consolidated = consolidate_layout(packed, PARAMS)
        assert _usable_area(consolidated) >= _usable_area(packed) - 1e-6


def _usable_area(layout, min_side=150.0):
    return sum(
        r.area
        for r in layout.remainders
        if r.width >= min_side and r.height >= min_side
    )


def test_is_deterministic():
    pieces = [
        Piece(id=f"p{i}", width=380, height=900, quantity=3, can_rotate=True)
        for i in range(4)
    ]
    (packed,) = _pack(pieces)
    first = consolidate_layout(packed, PARAMS)
    second = consolidate_layout(packed, PARAMS)
    assert first.to_dict() == second.to_dict()


def test_the_threshold_changes_which_tree_wins():
    """``min_usable_offcut`` is a real knob, not decoration.

    The same L-shaped waste can be carved two ways. Told that anything over
    50mm counts, the pass keeps the widest band it can (2050x2026 plus a
    1092x750); told that only large panels count, it keeps the full-height strip
    instead (1092x2780 plus a 954x2026). Neither is "more area" — they are
    different answers to what a retazo is worth here.
    """
    pieces = [
        Piece(id="lateral", width=475, height=750, quantity=2, can_rotate=False),
        Piece(id="travesano", width=120, height=1280, quantity=5, can_rotate=True),
        Piece(id="fondo", width=440, height=1535, quantity=1, can_rotate=False),
        Piece(id="puerta", width=215, height=2275, quantity=6, can_rotate=True),
        Piece(id="tapa", width=215, height=265, quantity=2, can_rotate=False),
    ]
    layouts = _pack(pieces)
    lenient = [
        _dims(consolidate_layout(board, PARAMS, min_usable_offcut=50.0))
        for board in layouts
    ]
    strict = [
        _dims(consolidate_layout(board, PARAMS, min_usable_offcut=900.0))
        for board in layouts
    ]
    assert lenient != strict


# --- the fallbacks -----------------------------------------------------------


def test_exhausted_budget_keeps_the_original_tree():
    """Budget is counted in nodes, never in wall clock, so this is reproducible."""
    pieces = [
        Piece(id=f"p{i}", width=320, height=2500, quantity=1, can_rotate=False)
        for i in range(6)
    ]
    (packed,) = _pack(pieces)
    starved = consolidate_layout(packed, PARAMS, node_budget=3)
    assert starved is packed


def test_a_layout_with_no_derivable_tree_keeps_the_original():
    """A placement this model cannot decompose must come back untouched.

    Two pieces overlapping the same band admit no separating cut. It cannot come
    out of the packer, but the pass runs on whatever it is handed and must never
    drop a leftover on the floor for it.
    """
    piece = Piece(id="a", width=500, height=500, quantity=1, can_rotate=False)
    other = Piece(id="b", width=500, height=500, quantity=1, can_rotate=False)
    (packed,) = _pack([piece])
    broken = CuttingLayout(
        material=SHEET,
        placed_pieces=[
            packed.placed_pieces[0],
            type(packed.placed_pieces[0])(
                piece=other, x=510.0, y=10.0, width=500.0, height=500.0
            ),
        ],
        remainders=[Rectangle(10.0, 520.0, 2050.0, 2270.0)],
        cuts=list(packed.cuts),
    )
    assert consolidate_layout(broken, PARAMS) is broken


def test_an_empty_board_is_returned_untouched():
    empty = CuttingLayout(material=SHEET, placed_pieces=[], remainders=[], cuts=[])
    assert consolidate_layout(empty, PARAMS) is empty
