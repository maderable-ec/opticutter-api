"""Unit tests for the material-pool solver (catalog board + finite offcuts).

Pure functions over the cutting engine — no DB. Cover the three fill orders,
finite offcut supply, the catalog fallback and the determinism of ``auto``.
"""

from src.cutting import CuttingParameters, PackingStrategy
from src.cutting.models import Piece
from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.pool import optimize_offcut_pool, optimize_pool
from src.modules.optimizations.schemas import PoolFillOrder

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
