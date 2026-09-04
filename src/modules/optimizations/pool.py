"""Multi-material pool packing: an anchor material + its attached (finite) offcuts.

A *pool* lets one group of pieces be cut across several sheets of the same
material. The anchor is the material the requirements point at, and it comes in
two shapes:

- **A catalog board** (infinite supply) plus client/company offcuts. Everything
  always fits, because another board can always be opened; the question is only
  how much of it gets bought. ``optimize_pool`` answers that.
- **An offcut** (finite supply) plus more offcuts — the client walks in with two
  retazos and a cut list. There is no infinite backstop, so "these pieces do not
  fit" is a legitimate answer and is returned rather than swallowed.
  ``optimize_offcut_pool`` answers that one.

Both reuse the pure cutting engine, so every resulting layout stays
single-material and flows unchanged through billing, persistence and the
proforma. Note the offcut half of ``optimize_pool`` is the one packing path that
never reaches the Rust kernel: the crate exports fills, not a single-bin packer,
and this needs both the ``unplaced`` list and the trims-exceed-the-sheet
``ValueError``.

Three fill orders (see ``PoolFillOrder``), all specific to a catalog anchor:
- ``offcuts_first``: fill the offcuts, then the catalog with the remainder.
- ``catalog_first``: use the fewest catalog boards such that the offcuts absorb
  the residual, so a big leftover lands on the client's offcut, not a bought
  board.
- ``auto``: compute both and keep the one with the least waste on the *catalog*
  (purchased) sheets. Deterministic, so the optimization hash stays stable.
"""

from typing import List, Optional, Tuple

from src.cutting.enums import PackingStrategy
from src.cutting.models import BinSpec, CuttingLayout, Material, Piece
from src.cutting.packer import GuillotineOptimizer
from src.cutting.parameters import CuttingParameters
from src.cutting.search import (
    ExactConfig,
    SearchBudget,
    downgrade_layout_to_half,
    optimize_bins,
)
from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.schemas import PoolFillOrder


def _domain_material(rm: ResolvedMaterial) -> Material:
    return Material(
        id=rm.key,
        width=rm.width,
        height=rm.height,
        thickness=rm.thickness,
        cost_per_unit=rm.cost_per_unit,
    )


def _pack_offcut(
    material: Material,
    pieces: List[Piece],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy,
    min_rect_size: float,
    sheet_number: int,
) -> Tuple[Optional[CuttingLayout], List[Piece]]:
    """Packs one physical offcut sheet; returns ``(layout or None, unplaced)``."""
    try:
        optimizer = GuillotineOptimizer(
            material=material,
            cutting_params=cutting_params,
            strategy=strategy,
            min_rect_size=min_rect_size,
        )
    except ValueError:
        # Trims exceed the offcut dimensions: unusable, nothing placed.
        return None, pieces
    placed, unplaced = optimizer.optimize(pieces)
    if not placed:
        return None, unplaced
    layout = CuttingLayout(
        material=material,
        placed_pieces=placed,
        remainders=optimizer.remainders,
        sheet_number=sheet_number,
        cuts=optimizer.cuts,
    )
    return layout, unplaced


def _fill_offcuts(
    offcuts: List[ResolvedMaterial],
    pieces: List[Piece],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy,
    min_rect_size: float,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Greedily fills each finite offcut sheet; returns ``(layouts, remaining)``."""
    layouts: List[CuttingLayout] = []
    remaining = pieces
    for offcut in offcuts:
        units = offcut.quantity or 1
        material = _domain_material(offcut)
        for unit in range(1, units + 1):
            if not remaining:
                return layouts, remaining
            layout, remaining = _pack_offcut(
                material,
                remaining,
                cutting_params,
                strategy,
                min_rect_size,
                sheet_number=unit,
            )
            # An empty sheet means no remaining piece fits this offcut size;
            # its other units won't fit either, so move to the next offcut.
            if layout is None:
                break
            layouts.append(layout)
    return layouts, remaining


def _fill_catalog(
    primary: ResolvedMaterial,
    pieces: List[Piece],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy,
    min_rect_size: float,
    max_sheets: int,
    budget: Optional[SearchBudget] = None,
    seed: int = 0,
    exact_config: Optional[ExactConfig] = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Packs the remainder onto catalog boards via the board-count search.

    Full boards only: the half-board downgrade happens once, on the final
    selection (see ``optimize_pool``), so the ``catalog_first`` probing keeps
    counting whole catalog sheets.
    """
    if not pieces or max_sheets <= 0:
        return [], pieces
    spec = BinSpec(
        key=primary.key,
        width=primary.width,
        height=primary.height,
        thickness=primary.thickness,
        cost_per_unit=primary.cost_per_unit,
    )
    return optimize_bins(
        pieces,
        [spec],
        cutting_params=cutting_params,
        strategy=strategy,
        budget=budget,
        seed=seed,
        min_rect_size=min_rect_size,
        max_sheets=max_sheets,
        exact_config=exact_config,
    )


def _offcuts_first(
    pieces: List[Piece],
    primary: ResolvedMaterial,
    offcuts: List[ResolvedMaterial],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy,
    min_rect_size: float,
    max_sheets: int,
    budget: Optional[SearchBudget] = None,
    seed: int = 0,
    exact_config: Optional[ExactConfig] = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    offcut_layouts, remaining = _fill_offcuts(
        offcuts, pieces, cutting_params, strategy, min_rect_size
    )
    catalog_layouts, unplaced = _fill_catalog(
        primary,
        remaining,
        cutting_params,
        strategy,
        min_rect_size,
        max_sheets,
        budget,
        seed,
        exact_config,
    )
    return offcut_layouts + catalog_layouts, unplaced


def _catalog_first(
    pieces: List[Piece],
    primary: ResolvedMaterial,
    offcuts: List[ResolvedMaterial],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy,
    min_rect_size: float,
    max_sheets: int,
    budget: Optional[SearchBudget] = None,
    seed: int = 0,
    exact_config: Optional[ExactConfig] = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Fewest catalog boards such that the offcuts absorb the residual.

    ``Nc`` = boards needed catalog-only (upper bound). We look for the smallest
    ``k`` in ``0..Nc`` where ``k`` catalog boards + the offcuts place every piece,
    so the residual (the tail that would otherwise sit on a bought board) lands on
    the client's offcuts. ``k = Nc`` always works (catalog alone fits all), so a
    solution is guaranteed.
    """
    catalog_only, oversized = _fill_catalog(
        primary,
        pieces,
        cutting_params,
        strategy,
        min_rect_size,
        max_sheets,
        budget,
        seed,
        exact_config,
    )
    nc = len(catalog_only)

    for k in range(nc + 1):
        catalog_layouts, remaining = _fill_catalog(
            primary,
            pieces,
            cutting_params,
            strategy,
            min_rect_size,
            k,
            budget,
            seed,
            exact_config,
        )
        offcut_layouts, remaining = _fill_offcuts(
            offcuts, remaining, cutting_params, strategy, min_rect_size
        )
        if not remaining:
            return catalog_layouts + offcut_layouts, oversized

    # Unreachable (k = nc places everything on catalog); guard for safety.
    return catalog_only, oversized


def _catalog_waste_score(
    layouts: List[CuttingLayout], catalog_key: str
) -> Tuple[float, int, int]:
    """Selection score (lower is better): waste on catalog sheets, then counts."""
    catalog = [layout for layout in layouts if layout.material.id == catalog_key]
    catalog_waste = sum(layout.waste_area for layout in catalog)
    return (catalog_waste, len(catalog), len(layouts))


def _apply_half_downgrade(
    layouts: List[CuttingLayout],
    half_spec: Optional[BinSpec],
    cutting_params: CuttingParameters,
    seed: int,
    min_rect_size: float,
    exact_config: Optional[ExactConfig] = None,
) -> List[CuttingLayout]:
    """Swaps catalog sheets whose content fits the half board (billing parity)."""
    if half_spec is None:
        return layouts
    out: List[CuttingLayout] = []
    for layout in layouts:
        if layout.material.id == half_spec.key and not layout.material.half_board:
            half = downgrade_layout_to_half(
                layout,
                half_spec,
                cutting_params=cutting_params,
                seed=seed,
                min_rect_size=min_rect_size,
                exact_config=exact_config,
            )
            out.append(half or layout)
        else:
            out.append(layout)
    return out


def optimize_pool(
    pieces: List[Piece],
    primary: ResolvedMaterial,
    offcuts: List[ResolvedMaterial],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy = PackingStrategy.MAX_EFFICIENCY,
    min_rect_size: float = 0.1,
    max_sheets: int = 100,
    half_spec: Optional[BinSpec] = None,
    budget: Optional[SearchBudget] = None,
    seed: int = 0,
    exact_config: Optional[ExactConfig] = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Packs ``pieces`` across the catalog board + its finite offcuts.

    Returns ``(layouts, unplaced)``: the combined single-material layouts (offcut
    sheets + catalog sheets) and whatever fit nowhere. With a catalog anchor the
    supply is unlimited, so ``unplaced`` only ever holds pieces larger than the
    board itself — but it is propagated rather than dropped, because the caller
    reports it and silently losing a piece is the failure mode that costs a
    re-cut.

    The fill order comes from ``primary.fill_order``; ``auto`` keeps whichever of
    ``offcuts_first``/``catalog_first`` wastes least catalog area (deterministic).
    ``half_spec`` (catalog materials only) enables the final half-board
    downgrade on the purchased sheets.
    """
    if not pieces:
        return [], []
    if not offcuts:
        layouts, unplaced = _fill_catalog(
            primary,
            pieces,
            cutting_params,
            strategy,
            min_rect_size,
            max_sheets,
            budget,
            seed,
            exact_config,
        )
        return (
            _apply_half_downgrade(
                layouts, half_spec, cutting_params, seed, min_rect_size, exact_config
            ),
            unplaced,
        )

    order = primary.fill_order
    args = (
        pieces,
        primary,
        offcuts,
        cutting_params,
        strategy,
        min_rect_size,
        max_sheets,
        budget,
        seed,
        exact_config,
    )

    if order == PoolFillOrder.offcuts_first:
        layouts, unplaced = _offcuts_first(*args)
    elif order == PoolFillOrder.catalog_first:
        layouts, unplaced = _catalog_first(*args)
    else:
        candidates = [_offcuts_first(*args), _catalog_first(*args)]
        layouts, unplaced = min(
            candidates, key=lambda c: _catalog_waste_score(c[0], primary.key)
        )
    return (
        _apply_half_downgrade(
            layouts, half_spec, cutting_params, seed, min_rect_size, exact_config
        ),
        unplaced,
    )


def _finite_spec(material: ResolvedMaterial) -> BinSpec:
    """Bin for one offcut: its geometry, its cost and how many of it exist."""
    return BinSpec(
        key=material.key,
        width=material.width,
        height=material.height,
        thickness=material.thickness,
        cost_per_unit=material.cost_per_unit,
        count=material.quantity or 1,
    )


def optimize_offcut_pool(
    pieces: List[Piece],
    anchor: ResolvedMaterial,
    offcuts: List[ResolvedMaterial],
    cutting_params: CuttingParameters,
    strategy: PackingStrategy = PackingStrategy.MAX_EFFICIENCY,
    min_rect_size: float = 0.1,
    max_sheets: int = 100,
    budget: Optional[SearchBudget] = None,
    seed: int = 0,
    exact_config: Optional[ExactConfig] = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Packs ``pieces`` across a pool with no catalog board: offcuts only.

    Every bin is finite, so unlike ``optimize_pool`` this can genuinely run out
    of material and the residual is the answer, not an error. The whole pool goes
    to ``optimize_bins`` as one heterogeneous bin set — rather than the greedy
    sheet-at-a-time fill ``optimize_pool`` uses for its offcuts — because here the
    offcuts *are* the problem: which piece lands on which retazo is the decision
    the seller is paying for, not a leftover detail after the boards are chosen.

    Bin order is anchor-then-attached, i.e. the order the request listed them, so
    the answer stays deterministic and cacheable.
    """
    if not pieces:
        return [], []
    specs = [_finite_spec(m) for m in (anchor, *offcuts)]

    def run(pool: List[Piece]) -> Tuple[List[CuttingLayout], List[Piece]]:
        return optimize_bins(
            pool,
            specs,
            cutting_params=cutting_params,
            strategy=strategy,
            budget=budget,
            seed=seed,
            min_rect_size=min_rect_size,
            max_sheets=max_sheets,
            exact_config=exact_config,
        )

    layouts, unplaced = run(pieces)
    if not unplaced:
        return layouts, unplaced

    # ``optimize_bins`` skips beam, LNS and restarts entirely when its greedy
    # baseline strands a piece, so what came back above is the plain sequential
    # fill. Re-running on the pieces that DID fit hands the search a feasible
    # instance and buys the packing quality back on the job that needs it most.
    # The placed set cannot shrink or grow: we remove exactly what the first pass
    # could not hold, and the ids are unique per instance (``_build_pieces``).
    #
    # Known limitation, deliberate: WHICH pieces are stranded therefore stays a
    # greedy decision. Answering "fit as many as possible" properly is a
    # different objective from the cost one the beam maximizes (it only ever
    # registers complete solutions), and the natural tool for it already exists
    # — ``exact.solve_bin(require_all=False)`` maximizes placed area. Left for
    # when the shop says the reported remainder is wrong, not before.
    stranded = {piece.id for piece in unplaced}
    kept = [piece for piece in pieces if piece.id not in stranded]
    if not kept:
        return layouts, unplaced
    repacked, spilled = run(kept)
    # A second pass that strands something the first one placed would report
    # fewer pieces than we can actually cut; keep the first plan in that case.
    return (layouts, unplaced) if spilled else (repacked, unplaced)
