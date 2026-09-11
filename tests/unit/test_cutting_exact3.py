"""CP-SAT 3-stage decision model (``src/cutting/exact3.py``).

Same two bars as ``test_cutting_exact.py`` — whatever comes back has to be
physically cuttable, and it has to be reproducible, because the optimizer's
payload is cached by input hash. The reproducibility half matters MORE here:
a 3-stage answer carries more arbitrary detail than a 2-stage one (the strip
height, the order of pieces inside a strip, the strip and column order), and
the engine has already paid once for inheriting a solver's arbitrary pick —
pre-order 21 billed 5 boards on macOS and 6 on linux with the same pinned
OR-Tools until ``exact._build_fill`` learned to re-derive the answer's form
from its content.
"""

import json

import pytest

from src.cutting import (
    BinSpec,
    CuttingLayout,
    CuttingParameters,
    Piece,
    exact3,
    exact_available,
)
from src.cutting.packer import expand_pieces
from tests.unit.cutting_invariants import assert_valid_fill, assert_valid_layouts

pytestmark = pytest.mark.skipif(
    not exact_available(), reason="OR-Tools not installed (exact endgame disabled)"
)

PARAMS = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)
FULL = BinSpec(key="board", width=2070, height=2800, thickness=15, cost_per_unit=88.48)
HALF = BinSpec(
    key="board",
    width=1035,
    height=2800,
    thickness=15,
    cost_per_unit=48.66,
    half_board=True,
)

# The BARROCO DORADO cut list: the case the 2-stage model cannot express. Kept
# in sync with ``BARROCO_DORADO_POOL`` in ``test_cutting_search.py``, which
# carries the full story and the cut trees that realize it.
BARROCO_DORADO_POOL = [
    (430, 850, 4),
    (565, 340, 4),
    (575, 340, 4),
    (460, 710, 2),
    (333, 710, 3),
    (435, 550, 4),
    (535, 110, 1),
    (80, 2780, 4),
    (300, 830, 1),
    (930, 790, 1),
    (525, 790, 1),
]


def _pool(spec, can_rotate=False):
    return expand_pieces(
        [
            Piece(id=f"p{i}", width=w, height=h, quantity=q, can_rotate=can_rotate)
            for i, (w, h, q) in enumerate(spec)
        ]
    )


def _as_layouts(fills):
    return [
        CuttingLayout(
            material=f.spec.to_material(),
            placed_pieces=f.placed,
            remainders=f.remainders,
            cuts=f.cuts,
        )
        for f in fills
    ]


def test_three_stage_fill_is_physically_cuttable():
    """Bounds, kerf separation and conservation — the same bar as the greedies."""
    pool = _pool([(500, 500, 6)], can_rotate=True)
    fill = exact3.fits_one_bin(pool, FULL, PARAMS)
    assert fill is not None
    assert_valid_fill(fill, PARAMS, len(pool))


def test_three_stage_fill_emits_cuts_and_leftovers():
    """A fill is a cut plan, not just coordinates: the saw needs both."""
    fill = exact3.fits_one_bin(_pool([(400, 600, 8)], can_rotate=True), FULL, PARAMS)
    assert fill is not None
    assert fill.cuts, "no saw segments emitted"
    assert all(cut.length > 0 for cut in fill.cuts)


def test_it_packs_a_strip_the_two_stage_model_cannot_express():
    """Pieces of DIFFERENT heights side by side — the whole reason this exists.

    Three pieces that only fit one board if they share a strip across its width.
    The 2-stage model gives each column one piece per row, so it cannot put them
    next to each other at all.
    """
    narrow = BinSpec(key="b", width=1000, height=900, thickness=15, cost_per_unit=1.0)
    pool = _pool([(300, 880, 1), (300, 500, 1), (350, 700, 1)])

    fill = exact3.fits_one_bin(pool, narrow, PARAMS)

    assert fill is not None, "the 3-stage model should place all three"
    assert len(fill.placed) == len(pool)
    assert_valid_fill(fill, PARAMS, len(pool))
    # They really are side by side: every piece starts at the same height.
    assert len({pp.y for pp in fill.placed}) == 1


def test_barroco_dorado_fits_one_board_and_a_half_without_rotating():
    """The commercial reference's partition, decided rather than stumbled upon."""
    pool = _pool(BARROCO_DORADO_POOL)

    fills = exact3.fits_bins(pool, [FULL, HALF], PARAMS)

    assert fills is not None
    assert sum(len(f.placed) for f in fills) == len(pool)
    layouts = _as_layouts(fills)
    assert_valid_layouts(layouts, [], PARAMS, len(pool))
    assert not any(pp.rotated for lay in layouts for pp in lay.placed_pieces)
    assert sum(lay.material.cost_per_unit for lay in layouts) == pytest.approx(
        137.14, abs=0.01
    )


def test_the_answer_is_reproducible():
    """Same inputs, same layout — the payload is cached by input hash."""

    def signature():
        fills = exact3.fits_bins(_pool(BARROCO_DORADO_POOL), [FULL, HALF], PARAMS)
        return json.dumps(
            [
                [
                    (pp.piece.id, pp.x, pp.y, pp.width, pp.height, pp.rotated)
                    for pp in f.placed
                ]
                for f in fills
            ],
            sort_keys=True,
        )

    assert signature() == signature()


def test_the_bill_does_not_depend_on_the_solver_tie_break(monkeypatch):
    """A different tied answer bills the same, even though it may LOOK different.

    Offsetting ``random_seed`` on every solve is the local proxy for another
    OR-Tools build — both change the arbitrary pick among equally valid answers.

    What is asserted is the honest claim, and the boundary is deliberate. This
    is a DECISION model: every feasible answer is equally good to it, so which
    pieces land on which sheet genuinely varies between builds and no amount of
    canonical reconstruction can fix that (making it canonical would mean adding
    a tie-break objective, i.e. turning the cheap decision into the expensive
    optimization — measured at 60s against 0.3s). What cannot vary is the bin
    multiset, because this module is only ever ASKED about a fixed one: the
    bill, which is the commercial claim, is identical by construction. Same
    boundary the engine already accepts for total cut length, one notch wider.
    """
    from ortools.sat.python import cp_model

    original = cp_model.CpSolver.Solve

    def contents(offset):
        def patched(self, model, *args, **kwargs):
            self.parameters.random_seed += offset
            return original(self, model, *args, **kwargs)

        monkeypatch.setattr(cp_model.CpSolver, "Solve", patched)
        fills = exact3.fits_bins(_pool(BARROCO_DORADO_POOL), [FULL, HALF], PARAMS)
        assert fills is not None
        assert sum(len(f.placed) for f in fills) == len(_pool(BARROCO_DORADO_POOL))
        return (
            sorted((f.spec.width, f.spec.height, f.spec.half_board) for f in fills),
            round(sum(f.spec.cost_per_unit for f in fills), 2),
        )

    assert contents(0) == contents(5)


def test_an_impossible_pool_is_refused_rather_than_mispacked():
    """More area than the bins hold: ``None``, never a partial answer."""
    pool = _pool([(1000, 2000, 6)])
    assert exact3.fits_bins(pool, [HALF], PARAMS) is None


def test_a_pool_bigger_than_the_cap_is_not_attempted():
    """The gate is a cheap question, not a whole-quote solver."""
    pool = _pool([(100, 100, exact3.MAX_PIECES + 1)])
    assert exact3.fits_bins(pool, [FULL], PARAMS) is None


def test_the_ladder_stops_when_its_budget_runs_out():
    """``None`` from an exhausted budget, not an unbounded search."""
    assert (
        exact3.fits_bins(
            _pool(BARROCO_DORADO_POOL),
            [FULL, HALF],
            PARAMS,
            total_deterministic_time=0.0,
        )
        is None
    )
