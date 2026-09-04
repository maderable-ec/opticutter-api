"""Unit tests for the material-pool solver (catalog board + finite offcuts).

Pure functions over the cutting engine — no DB. Cover the three fill orders,
finite offcut supply, the catalog fallback and the determinism of ``auto``.
"""

from src.cutting import CuttingParameters, PackingStrategy
from src.cutting.models import BinSpec, Piece
from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.pool import optimize_offcut_pool, optimize_pool
from src.modules.optimizations.schemas import PoolFillOrder
from tests.unit.cutting_invariants import assert_valid_layouts

PARAMS = CuttingParameters(kerf=3, top_trim=0, bottom_trim=0, left_trim=0, right_trim=0)


def _mat(
    key,
    width,
    height,
    *,
    source="catalog",
    cost=0.0,
    quantity=None,
    pool_key=None,
    fill_order=PoolFillOrder.auto,
    product_id=None,
):
    return ResolvedMaterial(
        key=key,
        width=width,
        height=height,
        thickness=18,
        cost_per_unit=cost,
        source=source,
        product_id=product_id,
        quantity=quantity,
        pool_key=pool_key,
        fill_order=fill_order,
    )


def _offcut(key, width, height, *, quantity=1, pool_key="board"):
    return _mat(
        key,
        width,
        height,
        source="clientOffcut",
        quantity=quantity,
        pool_key=pool_key,
    )


def _placed_ids(layouts, material_key):
    return sorted(
        pp.piece.id
        for layout in layouts
        for pp in layout.placed_pieces
        if layout.material.id == material_key
    )


def _pool(*args, **kwargs):
    """``optimize_pool`` for the catalog-anchored cases: nothing may be left over.

    A catalog board is unlimited, so an unplaced piece here means one bigger than
    the board — never the point of these tests. Asserting it on every call keeps
    the residual from being silently ignored the way it used to be.
    """
    layouts, unplaced = optimize_pool(*args, **kwargs)
    assert unplaced == []
    return layouts


def _count(layouts, material_key):
    return sum(1 for layout in layouts if layout.material.id == material_key)


def _all_placed_ids(layouts):
    return sorted(pp.piece.id for layout in layouts for pp in layout.placed_pieces)


def _catalog_waste(layouts, catalog_key):
    return sum(
        layout.waste_area for layout in layouts if layout.material.id == catalog_key
    )


def _signature(layouts):
    """Stable fingerprint: (material, sorted placed ids) per sheet, sorted."""
    return sorted(
        (layout.material.id, tuple(sorted(pp.piece.id for pp in layout.placed_pieces)))
        for layout in layouts
    )


def test_offcuts_first_fills_offcut_then_catalog():
    primary = _mat("board", 2440, 1220, fill_order=PoolFillOrder.offcuts_first)
    offcuts = [_offcut("off1", 800, 600)]
    pieces = [
        Piece(id="big", width=2000, height=1000),
        Piece(id="small", width=500, height=400),
    ]

    layouts = _pool(pieces, primary, offcuts, PARAMS)

    # Small piece lands on the client's offcut; the big one on a catalog board.
    assert _placed_ids(layouts, "off1") == ["small"]
    assert "big" in _placed_ids(layouts, "board")
    assert _all_placed_ids(layouts) == ["big", "small"]


def test_no_offcuts_falls_back_to_catalog_only():
    primary = _mat("board", 2440, 1220)
    pieces = [
        Piece(id="a", width=700, height=500),
        Piece(id="b", width=700, height=500),
    ]

    layouts = _pool(pieces, primary, [], PARAMS)

    assert layouts, "expected at least one catalog sheet"
    assert all(layout.material.id == "board" for layout in layouts)
    assert _all_placed_ids(layouts) == ["a", "b"]


def test_offcut_finite_quantity_is_respected():
    # Offcut holds a single 700x500 per sheet; only 2 units are available, so the
    # third identical piece must spill onto a catalog board.
    primary = _mat("board", 2440, 1220, fill_order=PoolFillOrder.offcuts_first)
    offcuts = [_offcut("off1", 800, 600, quantity=2)]
    pieces = [Piece(id=f"p{i}", width=700, height=500) for i in range(3)]

    layouts = _pool(pieces, primary, offcuts, PARAMS)

    assert _count(layouts, "off1") == 2
    assert _count(layouts, "board") == 1
    assert _all_placed_ids(layouts) == ["p0", "p1", "p2"]


def test_catalog_first_pushes_residual_onto_offcut():
    # Three 900x900 pieces: one per catalog board (Nc=3). With catalog_first the
    # solver uses the fewest catalog boards such that the offcut absorbs the tail.
    primary = _mat("board", 1000, 1000, fill_order=PoolFillOrder.catalog_first)
    offcuts = [_offcut("off1", 950, 950, quantity=1)]
    pieces = [Piece(id=f"q{i}", width=900, height=900) for i in range(3)]

    layouts = _pool(pieces, primary, offcuts, PARAMS)

    assert _count(layouts, "board") == 2
    assert _count(layouts, "off1") == 1
    assert _all_placed_ids(layouts) == ["q0", "q1", "q2"]


def test_auto_minimizes_catalog_waste_and_is_deterministic():
    pieces = [
        Piece(id="big", width=1900, height=900),
        Piece(id="mid", width=900, height=900),
        Piece(id="tiny", width=300, height=300),
    ]
    offcuts = [_offcut("off1", 1000, 1000, quantity=1)]

    auto = _pool(
        pieces,
        _mat("board", 2000, 1000, fill_order=PoolFillOrder.auto),
        offcuts,
        PARAMS,
    )
    off_first = _pool(
        pieces,
        _mat("board", 2000, 1000, fill_order=PoolFillOrder.offcuts_first),
        offcuts,
        PARAMS,
    )

    # auto keeps whichever candidate wastes the least catalog area.
    assert _catalog_waste(auto, "board") <= _catalog_waste(off_first, "board")
    assert _all_placed_ids(auto) == ["big", "mid", "tiny"]

    # Deterministic: same inputs → identical layout signature (cache-safe hash).
    again = _pool(
        pieces,
        _mat("board", 2000, 1000, fill_order=PoolFillOrder.auto),
        offcuts,
        PARAMS,
    )
    assert _signature(auto) == _signature(again)


def test_long_offcuts_strategy_threads_through():
    # The packing strategy is forwarded to both the offcut and catalog passes.
    primary = _mat("board", 2440, 1220, fill_order=PoolFillOrder.offcuts_first)
    offcuts = [_offcut("off1", 800, 600)]
    pieces = [
        Piece(id="a", width=500, height=400),
        Piece(id="b", width=2000, height=1000),
    ]

    layouts = _pool(
        pieces, primary, offcuts, PARAMS, strategy=PackingStrategy.LONG_OFFCUTS
    )

    assert _all_placed_ids(layouts) == ["a", "b"]


# --- Offcut-only pool: no catalog board, finite supply -----------------------


def test_offcut_only_pool_spreads_pieces_across_both_retazos():
    # The seller's case: a cut list and two of the client's retazos, no board.
    anchor = _mat("r1", 1000, 1000, source="clientOffcut", quantity=1)
    offcuts = [_mat("r2", 1000, 1000, source="clientOffcut", quantity=1, pool_key="r1")]
    pieces = [Piece(id=f"p{i}", width=900, height=900) for i in range(2)]

    layouts, unplaced = optimize_offcut_pool(pieces, anchor, offcuts, PARAMS)

    assert unplaced == []
    assert _count(layouts, "r1") == 1
    assert _count(layouts, "r2") == 1
    assert _all_placed_ids(layouts) == ["p0", "p1"]


def test_offcut_only_pool_never_invents_a_sheet():
    # One retazo, three pieces that each need their own: two cannot be cut, and
    # saying so is the answer. Before finite supply reached the search, the
    # engine happily emitted three copies of a retazo the client owns once.
    anchor = _mat("r1", 1000, 1000, source="clientOffcut", quantity=1)
    pieces = [Piece(id=f"p{i}", width=900, height=900) for i in range(3)]

    layouts, unplaced = optimize_offcut_pool(pieces, anchor, [], PARAMS)

    assert _count(layouts, "r1") == 1
    assert len(unplaced) == 2
    assert len(_all_placed_ids(layouts)) == 1
    # Conservation: every piece is either cut or reported, never dropped.
    assert sorted(_all_placed_ids(layouts) + [p.id for p in unplaced]) == [
        "p0",
        "p1",
        "p2",
    ]


def test_offcut_only_pool_honours_each_quantity():
    anchor = _mat("r1", 1000, 1000, source="clientOffcut", quantity=2)
    offcuts = [_mat("r2", 1000, 1000, source="clientOffcut", quantity=1, pool_key="r1")]
    pieces = [Piece(id=f"p{i}", width=900, height=900) for i in range(5)]

    layouts, unplaced = optimize_offcut_pool(pieces, anchor, offcuts, PARAMS)

    assert _count(layouts, "r1") == 2
    assert _count(layouts, "r2") == 1
    assert len(unplaced) == 2


def test_offcut_only_pool_is_deterministic():
    # The payload is cached by input hash, so two runs must be byte-identical.
    anchor = _mat("r1", 1200, 800, source="clientOffcut", quantity=1)
    offcuts = [_mat("r2", 900, 700, source="clientOffcut", quantity=2, pool_key="r1")]
    pieces = [
        Piece(id=f"p{i}", width=300 + 40 * i, height=250 + 30 * i) for i in range(9)
    ]

    first, first_rest = optimize_offcut_pool(pieces, anchor, offcuts, PARAMS)
    again, again_rest = optimize_offcut_pool(pieces, anchor, offcuts, PARAMS)

    assert _signature(first) == _signature(again)
    assert [p.id for p in first_rest] == [p.id for p in again_rest]


def test_offcut_only_pool_searches_even_though_the_material_is_free():
    """A free pool must still be packed, not just filled.

    ``_cost_lower_bound`` collapses to 0 as soon as a bin costs nothing, so
    matching it used to "prove" optimality before the beam ever opened and the
    plan came straight out of the sequential greedy. These six pieces are the
    minimal case that exposes it: the greedy spills onto the second retazo, the
    search fits all six on the first and leaves the client's other retazo whole.
    """
    anchor = _mat("r1", 1220, 1000, source="clientOffcut", quantity=1)
    offcuts = [_mat("r2", 900, 800, source="clientOffcut", quantity=1, pool_key="r1")]
    dims = [(240, 330), (590, 440), (290, 600), (360, 420), (580, 430), (500, 270)]
    pieces = [Piece(id=f"p{i}", width=w, height=h) for i, (w, h) in enumerate(dims)]

    layouts, unplaced = optimize_offcut_pool(pieces, anchor, offcuts, PARAMS)

    assert unplaced == []
    assert _count(layouts, "r2") == 0, "the second retazo should stay untouched"
    assert _count(layouts, "r1") == 1
    assert len(_all_placed_ids(layouts)) == 6


# --- Offcut-only pool: rotation must never cost placed pieces ----------------
#
# The shop's report on pre-order 7: five pieces did not fit, the seller ticked
# "Rotar" hoping a couple more would land in the leftover, and the engine turned
# every piece sideways and fit *fewer*. Three things caused it and all three are
# exercised here: the search is skipped whenever the sequential greedy strands a
# piece (``search.optimize_bins``), which pieces get stranded was therefore a
# greedy accident, and ``packer._place_piece`` decides orientation by a fixed
# tie-break that always prefers the wider one — never by search.

P7_PARAMS = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)


def _preorder_7(can_rotate):
    """Pre-order 7 verbatim: two client retazos, 20 pieces of 200x700."""
    anchor = _mat("A", 1200, 1500, source="clientOffcut", quantity=1)
    offcuts = [_mat("B", 1100, 1300, source="clientOffcut", quantity=1, pool_key="A")]
    pieces = [
        Piece(id=f"p{i}", width=200, height=700, can_rotate=can_rotate)
        for i in range(20)
    ]
    return pieces, anchor, offcuts


def _run_pool(pieces, anchor, offcuts, params):
    """Runs the pool and asserts the invariants every plan owes, then reports."""
    layouts, unplaced = optimize_offcut_pool(pieces, anchor, offcuts, params)
    assert_valid_layouts(layouts, unplaced, params, len(pieces))
    assert sorted(_all_placed_ids(layouts) + [p.id for p in unplaced]) == sorted(
        p.id for p in pieces
    )
    return layouts, unplaced


def _placed_count(layouts):
    return len(_all_placed_ids(layouts))


def test_allowing_rotation_never_places_fewer_pieces_preorder_7():
    """Permission may not cost yield: ``can_rotate`` is a *may*, not a *must*.

    Forbidding rotation is a strict restriction of allowing it, so any plan the
    forbidden pool admits is also legal for the permitted one. The engine used
    to answer 13 with rotation against 15 without it.
    """
    locked, unplaced_locked = _run_pool(*_preorder_7(False), P7_PARAMS)
    free, unplaced_free = _run_pool(*_preorder_7(True), P7_PARAMS)

    assert _placed_count(locked) == 15, "grain-locked baseline moved; re-read the case"
    assert _placed_count(free) >= _placed_count(locked)
    assert len(unplaced_free) <= len(unplaced_locked)


def test_preorder_7_cuts_eighteen_pieces_on_two_client_offcuts():
    """The yield the existing portfolio already reaches, once it is consulted.

    ``GreedyConfig(sort="area", split=LONGER_AXIS)`` packs 11 on the 1200x1500
    and 7 on the 1100x1300. A floor, not an equality: a future improvement must
    not turn this red.
    """
    layouts, unplaced = _run_pool(*_preorder_7(True), P7_PARAMS)

    assert _placed_count(layouts) >= 18
    assert len(unplaced) <= 2


def test_rotation_monotonicity_over_a_seeded_population():
    """The same "permission never costs yield" property, over small seeded pools."""
    import random

    rng = random.Random(20260904)
    for _ in range(30):
        anchor = _mat(
            "A",
            rng.choice([900, 1100, 1200]),
            rng.choice([700, 800, 1000]),
            source="clientOffcut",
            quantity=1,
        )
        offcuts = [
            _mat(
                "B",
                rng.choice([600, 900]),
                rng.choice([500, 700]),
                source="clientOffcut",
                quantity=1,
                pool_key="A",
            )
        ]
        dims = [
            (rng.randrange(150, 500, 10), rng.randrange(150, 600, 10))
            for _ in range(rng.randint(5, 10))
        ]
        locked = [
            Piece(id=f"p{i}", width=w, height=h, can_rotate=False)
            for i, (w, h) in enumerate(dims)
        ]
        free = [
            Piece(id=f"p{i}", width=w, height=h, can_rotate=True)
            for i, (w, h) in enumerate(dims)
        ]

        locked_layouts, _ = _run_pool(locked, anchor, offcuts, P7_PARAMS)
        free_layouts, _ = _run_pool(free, anchor, offcuts, P7_PARAMS)

        assert _placed_count(free_layouts) >= _placed_count(locked_layouts), dims


def test_rotation_is_not_gratuitous():
    """Where turning pieces buys nothing, the plan leaves them as drawn.

    Four 300x400 on an 830x830 retazo fit two-by-two in either orientation, yet
    the packer's tie-break (``rect_w - placed_width``, minimized) turned all
    four sideways. Nothing is gained and the operator reads a diagram that no
    longer matches the cut list he typed.
    """
    anchor = _mat("A", 830, 830, source="clientOffcut", quantity=1)
    pieces = [Piece(id=f"p{i}", width=300, height=400) for i in range(4)]

    layouts, unplaced = _run_pool(pieces, anchor, [], P7_PARAMS)

    assert unplaced == []
    assert _placed_count(layouts) == 4
    assert not any(pp.rotated for layout in layouts for pp in layout.placed_pieces)


def test_finite_pool_plan_is_deterministic_with_rotation():
    """The payload is cached by input hash: two runs must be byte-identical."""
    first, first_rest = _run_pool(*_preorder_7(True), P7_PARAMS)
    again, again_rest = _run_pool(*_preorder_7(True), P7_PARAMS)

    assert _signature(first) == _signature(again)
    assert [p.id for p in first_rest] == [p.id for p in again_rest]


def test_finite_pool_is_never_worse_than_the_sequential_fill():
    """Strictly additive: the extra candidates can only be adopted when better.

    Scored with the same objective the pool uses to choose, against the plan the
    legacy sequential fill alone produces.
    """
    from src.cutting.search import finite_plan_objective, optimize_bins

    pieces, anchor, offcuts = _preorder_7(True)
    specs = [
        BinSpec(key="A", width=1200, height=1500, thickness=18, count=1),
        BinSpec(key="B", width=1100, height=1300, thickness=18, count=1),
    ]
    baseline_layouts, baseline_rest = optimize_bins(pieces, specs, P7_PARAMS)
    layouts, unplaced = _run_pool(pieces, anchor, offcuts, P7_PARAMS)

    assert finite_plan_objective(layouts, unplaced) <= finite_plan_objective(
        baseline_layouts, baseline_rest
    )
