"""CP-SAT exact endgame (``src/cutting/exact.py``).

The solver is trusted with geometry the saw has to reproduce, so these tests
care about two things above all: that whatever it returns is *physically
cuttable* (same invariants as every other constructor), and that it is
*reproducible* — the optimizer payload is cached by input hash, so a solver that
answered differently on a second run would silently serve two customers two
different cutting plans for one quote.
"""

import json

import pytest

from src.cutting import BinSpec, CuttingParameters, Piece, exact_available
from src.cutting.exact import fits_one_bin, solve_bin
from src.cutting.packer import expand_pieces
from tests.unit.cutting_invariants import assert_valid_fill

pytestmark = pytest.mark.skipif(
    not exact_available(), reason="OR-Tools not installed (exact endgame disabled)"
)

PARAMS = CuttingParameters(
    kerf=5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)
BOARD = BinSpec(key="board", width=2070, height=2800, thickness=15, cost_per_unit=58.0)


def _pool(width, height, quantity, can_rotate=True):
    return expand_pieces(
        [
            Piece(
                id="p",
                width=width,
                height=height,
                quantity=quantity,
                can_rotate=can_rotate,
            )
        ]
    )


def test_exact_fill_is_physically_cuttable():
    """Bounds, kerf separation and conservation — the same bar as the greedies."""
    pool = _pool(500, 500, 6)
    fill = fits_one_bin(pool, BOARD, PARAMS)
    assert fill is not None
    assert_valid_fill(fill, PARAMS, len(pool))


def test_exact_fill_emits_cuts_and_leftovers():
    """A fill is a cut plan, not just coordinates: the saw needs both."""
    fill = fits_one_bin(_pool(400, 600, 8), BOARD, PARAMS)
    assert fill is not None
    assert fill.cuts, "no saw segments emitted"
    assert fill.remainders, "no leftover rectangles emitted"
    assert all(cut.length > 0 for cut in fill.cuts)


def test_exact_refuses_a_pool_that_exceeds_the_board():
    """More area than the board has: no fill, rather than a partial one."""
    assert fits_one_bin(_pool(1000, 1000, 12), BOARD, PARAMS) is None


def test_exact_never_rotates_a_grain_locked_piece():
    """``can_rotate=False`` is a material property (veta), not a hint."""
    pool = _pool(700, 1200, 4, can_rotate=False)
    fill = fits_one_bin(pool, BOARD, PARAMS)
    assert fill is not None
    assert all(not pp.rotated for pp in fill.placed)
    assert_valid_fill(fill, PARAMS, len(pool))


def test_exact_handles_fractional_millimetres():
    """Sub-millimetre dims round conservatively, so the layout is still valid."""
    params = CuttingParameters(
        kerf=4.5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
    )
    pool = _pool(333.5, 610.25, 8)
    fill = fits_one_bin(pool, BOARD, params)
    assert fill is not None
    assert_valid_fill(fill, params, len(pool))


def test_exact_maximize_places_a_dense_subset():
    """Orphan-absorption mode: fill the board hard, leave the rest for later."""
    pool = _pool(370, 570, 60)
    fill = solve_bin(pool, BOARD, PARAMS, require_all=False)
    assert fill is not None
    assert 0 < len(fill.placed) < len(pool)
    usable = (BOARD.width - 20) * (BOARD.height - 20)
    # 3 columns of 570 + 7 rows of 370 each is the 2-stage optimum for this pool;
    # the remaining strip is too narrow for a fourth column.
    assert fill.used_area / usable > 0.80
    assert_valid_fill(fill, PARAMS, len(fill.placed))


def test_exact_is_deterministic():
    """Same model twice -> byte-identical placements (the cache depends on it)."""
    pool = _pool(370, 570, 18)

    def run():
        fill = fits_one_bin(pool, BOARD, PARAMS)
        assert fill is not None
        return json.dumps([pp.to_dict() for pp in fill.placed], sort_keys=True)

    assert run() == run()


# A pool with several equally-optimal 2-stage packings: columns of equal width
# that can be permuted, and same-height pieces that can swap between columns.
# Measured: before the reconstruction was canonicalized, six seed offsets
# produced six different layouts of this pool.
TIE_PRONE = [(500, 900, 6), (500, 600, 4), (300, 900, 3)]


def _tie_prone_pool():
    return expand_pieces(
        [
            Piece(id=f"t{i}", width=w, height=h, quantity=q, can_rotate=False)
            for i, (w, h, q) in enumerate(TIE_PRONE)
        ]
    )


@pytest.mark.parametrize("offset", range(6))
def test_exact_layout_is_canonical_whatever_tied_optimum_comes_back(
    monkeypatch, offset
):
    """Tight, ordered columns on every tie-break — the build-independence fix.

    Perturbing ``random_seed`` stands in for running a different OR-Tools
    *build*: both change which of many equally-optimal answers the solver hands
    back. Two things it reports are arbitrary rather than meaningful — the
    column index, and ``widths[k]``, which the model only bounds from below, so
    a 425mm column could come back declared 505mm. Reconstruction re-derives
    both from the content, and that has to hold under any tie-break: on
    pre-order 21 at kerf 5 this leak cost a whole board on the build that ships
    (see ``tests/unit/test_cutting_search.py``).
    """
    from ortools.sat.python import cp_model

    original = cp_model.CpSolver.Solve

    def perturbed(self, model, *args, **kwargs):
        self.parameters.random_seed = self.parameters.random_seed + offset
        return original(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "Solve", perturbed)

    pool = _tie_prone_pool()
    fill = solve_bin(pool, BOARD, PARAMS, require_all=False, transposed=False)
    assert fill is not None
    assert_valid_fill(fill, PARAMS, len(fill.placed))

    columns = {}
    for pp in fill.placed:
        columns.setdefault(round(pp.x, 6), []).append(pp)
    xs = sorted(columns)

    # No phantom slack: a column is exactly as wide as its widest piece, so
    # consecutive columns sit exactly one kerf apart.
    widths = [max(pp.width for pp in columns[x]) for x in xs]
    for x, width, next_x in zip(xs, widths, xs[1:]):
        assert next_x == pytest.approx(x + width + PARAMS.kerf)

    # Equal-width columns are interchangeable, so the order has to be pinned.
    assert widths == sorted(widths, reverse=True)


def test_exact_respects_a_narrower_bin():
    """The half board is a different bin, not a scaled one: it must be honored."""
    half = BinSpec(
        key="board",
        width=1035,
        height=2800,
        thickness=15,
        cost_per_unit=31.90,
        half_board=True,
    )
    fill = fits_one_bin(_pool(500, 900, 6), half, PARAMS)
    assert fill is not None
    assert_valid_fill(fill, PARAMS, 6)
    assert all(
        pp.x + pp.width <= half.width - PARAMS.right_trim + 1e-6 for pp in fill.placed
    )
