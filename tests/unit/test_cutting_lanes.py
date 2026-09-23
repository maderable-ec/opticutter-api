"""The full-height-lane plan candidate (``src/cutting/lanes.py``).

The case that motivated the module is pre-order 132: 54 pieces of MDP
2.80x2.07 the engine billed as 3 whole boards, which fit on 2 boards + a half.
It is a *partition* problem — the packing of each sheet was never the issue —
so the tests here pin the partition, the guards that keep the candidate from
ever proposing something worse, and the physical validity of what it emits.
"""

import pytest

from src.cutting import BinSpec, CuttingParameters, Piece
from src.cutting.lanes import lane_plan
from tests.unit.cutting_invariants import assert_valid_fill

PARAMS = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)

PRICE = 88.478261
FULL = BinSpec(key="board", width=2070, height=2800, thickness=15, cost_per_unit=PRICE)
HALF = BinSpec(
    key="board",
    width=1035,
    height=2800,
    thickness=15,
    cost_per_unit=PRICE / 2 * 1.05,
    half_board=True,
)
THREE_FULL = 3 * PRICE
TWO_AND_A_HALF = 2 * FULL.cost_per_unit + HALF.cost_per_unit

# (width, height, quantity, can_rotate) — pre-order 132 verbatim.
PREORDER_132 = [
    (620, 2625, 3, False),
    (220, 2565, 3, False),
    (222, 607, 30, False),
    (760, 120, 3, False),
    (600, 120, 3, False),
    (222, 622, 6, False),
    (622, 222, 6, True),
]


def _pieces(spec):
    out = []
    for i, (w, h, q, rot) in enumerate(spec):
        for k in range(q):
            out.append(
                Piece(id=f"p{i}#{k}", width=w, height=h, quantity=1, can_rotate=rot)
            )
    return out


def _cost(plan):
    return sum(fill.spec.cost_per_unit for fill in plan)


def test_preorder_132_fits_on_two_boards_and_a_half():
    pieces = _pieces(PREORDER_132)

    plan = lane_plan(pieces, [FULL, HALF], PARAMS, below_cost=THREE_FULL)

    assert plan is not None
    assert sum(len(fill.placed) for fill in plan) == len(pieces)
    assert _cost(plan) == pytest.approx(TWO_AND_A_HALF)
    assert [fill.spec.half_board for fill in plan].count(True) == 1


def test_emitted_sheets_are_physically_valid():
    pieces = _pieces(PREORDER_132)

    plan = lane_plan(pieces, [FULL, HALF], PARAMS, below_cost=THREE_FULL)

    for fill in plan:
        assert_valid_fill(fill, PARAMS, len(fill.placed))


def test_is_deterministic():
    pieces = _pieces(PREORDER_132)

    def signature():
        plan = lane_plan(pieces, [FULL, HALF], PARAMS, below_cost=THREE_FULL)
        return [
            (
                fill.spec.width,
                [(p.piece.id, p.x, p.y, p.width, p.height) for p in fill.placed],
            )
            for fill in plan
        ]

    assert signature() == signature()


def test_never_proposes_a_plan_at_or_above_the_incumbent():
    """``below_cost`` is a hard ceiling: it is the whole safety argument."""
    pieces = _pieces(PREORDER_132)

    assert lane_plan(pieces, [FULL, HALF], PARAMS, below_cost=TWO_AND_A_HALF) is None


def test_ignores_bins_of_finite_supply():
    """A finite offcut pool is ``pool.py``'s problem, not a 1-D width question."""
    pieces = _pieces(PREORDER_132)
    offcut = BinSpec(
        key="board", width=2070, height=2800, thickness=15, cost_per_unit=0.0, count=2
    )

    assert lane_plan(pieces, [offcut], PARAMS, below_cost=THREE_FULL) is None


def test_ignores_a_half_that_does_not_share_the_usable_height():
    """A board halved across its short side has no full-height sibling.

    Plywood and ranurado are halved that way; a lane is full-height by
    definition, so the candidate simply declines rather than guessing.
    """
    pieces = _pieces(PREORDER_132)
    short_half = BinSpec(
        key="board",
        width=2070,
        height=1400,
        thickness=15,
        cost_per_unit=HALF.cost_per_unit,
        half_board=True,
    )

    plan = lane_plan(pieces, [FULL, short_half], PARAMS, below_cost=THREE_FULL)

    assert plan is None or all(not fill.spec.half_board for fill in plan)


def test_lays_a_rotatable_piece_down_to_free_a_lane():
    """The move that decides 132, isolated.

    The six 622x222 arrive lying down. Standing all six needs 17 lanes and 3
    boards; leaving two of them lying in a strip needs 16 and fits 2 + a half.
    With the rotatables pinned upright there is no such choice, and the
    candidate cannot beat three boards.
    """
    upright = [
        (w, h, q, False) if (w, h) == (622, 222) else (w, h, q, rot)
        for (w, h, q, rot) in PREORDER_132
    ]

    assert (
        lane_plan(_pieces(upright), [FULL, HALF], PARAMS, below_cost=THREE_FULL) is None
    )
    assert lane_plan(_pieces(PREORDER_132), [FULL, HALF], PARAMS, below_cost=THREE_FULL)


def test_returns_none_when_a_piece_fits_no_bin():
    oversized = [Piece(id="huge", width=3000, height=3000, quantity=1)]

    assert lane_plan(oversized, [FULL, HALF], PARAMS, below_cost=THREE_FULL) is None


def test_declines_when_the_strip_cannot_take_what_the_lanes_displaced():
    """The failure mode this module is most exposed to, pinned at its boundary.

    Sending a width group to the strips is what buys the cheaper width split,
    but the strip is only as tall as what the tallest lane leaves over — and
    nothing checks that until the pieces are already out of the lanes. Measured
    over the 150 real catalog pools in the shop's database, this is where 743 of
    1463 candidates die, so it is the common path and not an edge case.

    Three 680x2700 lanes fill one sheet (2052 of 2050+kerf) and leave a 76 mm
    strip. A 1400x50 rail fits it and the plan closes on one board; the same
    rail at 100 mm does not, and the only remaining split costs two boards,
    which is not cheaper than the incumbent. The answer then is ``None`` --
    never a plan with the rail quietly left out.
    """

    def plan_with_rail(rail_height):
        pieces = [
            Piece(id=f"lane{i}", width=680, height=2700, quantity=1, can_rotate=False)
            for i in range(3)
        ]
        pieces.append(
            Piece(
                id="rail", width=1400, height=rail_height, quantity=1, can_rotate=False
            )
        )
        return pieces, lane_plan(pieces, [FULL, HALF], PARAMS, below_cost=2 * PRICE)

    pieces, fits = plan_with_rail(50)
    assert fits is not None
    assert sum(len(fill.placed) for fill in fits) == len(pieces)
    assert _cost(fits) == pytest.approx(PRICE)

    _pieces_too_tall, declined = plan_with_rail(100)
    assert declined is None


def test_a_corrupt_plan_is_refused_even_when_it_bills_less(monkeypatch):
    """The last gate: cheaper is not the same as cuttable.

    ``_emit`` computes its coordinates by hand, and neither the cost comparison
    nor the piece count upstream can tell a cheaper plan from one that stacks
    two pieces on the same square millimetre — a plan can hold every piece and
    still be uncuttable. This nudges one placement into its neighbour and
    asserts the candidate is dropped rather than shipped, which is the failure
    that would reach the saw instead of the bill.
    """
    from src.cutting import lanes

    real_emit = lanes._emit

    def emit_overlapping(sheet, strip, params, min_rect_size):
        fill = real_emit(sheet, strip, params, min_rect_size)
        if len(fill.placed) > 1:
            # Slide one piece on top of its neighbour.
            fill.placed[1].x = fill.placed[0].x
            fill.placed[1].y = fill.placed[0].y
        return fill

    monkeypatch.setattr(lanes, "_emit", emit_overlapping)

    assert (
        lane_plan(_pieces(PREORDER_132), [FULL, HALF], PARAMS, below_cost=THREE_FULL)
        is None
    )
