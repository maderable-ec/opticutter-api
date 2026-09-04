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

from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting.enums import PackingStrategy
from src.cutting.models import BinSpec, CuttingLayout, Material, Piece
from src.cutting.packer import GuillotineOptimizer
from src.cutting.parameters import CuttingParameters
from src.cutting.search import (
    ExactConfig,
    SearchBudget,
    downgrade_layout_to_half,
    fill_finite_bins_max_yield,
    finite_plan_objective,
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


def _without_rotation(pieces: Sequence[Piece]) -> List[Piece]:
    """The same pool with the permission to rotate revoked.

    ``can_rotate`` is a permission, not an obligation, so this pool is a strict
    restriction of the original: every plan it admits is also legal for the
    original, and every placement it emits is physically cuttable as drawn. That
    is what makes it a free extra candidate rather than a different question.
    """
    return [
        Piece(
            id=piece.id,
            width=piece.width,
            height=piece.height,
            quantity=piece.quantity,
            can_rotate=False,
            priority=piece.priority,
        )
        for piece in pieces
    ]


def _plan_has_rotation(layouts: Sequence[CuttingLayout]) -> bool:
    return any(pp.rotated for layout in layouts for pp in layout.placed_pieces)


def _rebind_to_original(
    layouts: List[CuttingLayout],
    unplaced: List[Piece],
    by_id: Dict[str, Piece],
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Puts the caller's own ``Piece`` objects back into a clone-built plan.

    Only the ``piece`` reference changes -- geometry, position and ``rotated``
    are the clone's answer and stay verbatim. It matters because ``rotated`` is
    read against ``piece.can_rotate`` downstream (the shared validity check, and
    ``service._geometric_edges`` when remapping edge banding), and because the
    caller identifies pieces by object as well as by id.
    """
    for layout in layouts:
        for pp in layout.placed_pieces:
            original = by_id.get(pp.piece.id)
            if original is not None:
                pp.piece = original
    return layouts, [by_id.get(p.id, p) for p in unplaced]


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

    The plan is chosen from a small portfolio of candidates rather than taken from
    a single pipeline, and ``finite_plan_objective`` decides — a tie always keeps
    the incumbent, so every candidate is strictly additive. Two earn their place:
    the pool with rotation revoked (``_without_rotation``), because allowing a
    piece to turn must never place fewer than forbidding it and it used to; and
    ``fill_finite_bins_max_yield``, because once the stock runs out the cost
    search cannot rank the answer at all — it only compares plans that place
    everything, and it skips its own search the moment the baseline strands a
    piece.
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

    def plan(pool: List[Piece]) -> Tuple[List[CuttingLayout], List[Piece]]:
        """Fill, then re-search on whatever actually fit."""
        layouts, unplaced = run(pool)
        if not unplaced:
            return layouts, unplaced

        # ``optimize_bins`` skips beam, LNS and restarts entirely when its greedy
        # baseline strands a piece, so what came back above is the plain
        # sequential fill. Re-running on the pieces that DID fit hands the search
        # a feasible instance and buys the packing quality back on the job that
        # needs it most. The placed set cannot shrink or grow: we remove exactly
        # what the first pass could not hold, and the ids are unique per instance
        # (``_build_pieces``).
        #
        # WHICH pieces are stranded is still this pass's greedy decision; what
        # corrects it is the candidate portfolio below.
        stranded = {piece.id for piece in unplaced}
        kept = [piece for piece in pool if piece.id not in stranded]
        if not kept:
            return layouts, unplaced
        repacked, spilled = run(kept)
        # A second pass that strands something the first one placed would report
        # fewer pieces than we can actually cut; keep the first plan in that case.
        return (layouts, unplaced) if spilled else (repacked, unplaced)

    def max_yield(pool: List[Piece]) -> Tuple[List[CuttingLayout], List[Piece]]:
        """Cut as much as the retazos hold, then re-search over what was cut.

        The yield pass answers "how much fits"; the refinement asks the cost
        objective to lay those same pieces out on as few sheets as it can. A
        refinement that strands something is discarded outright -- reporting
        fewer pieces than we just proved cuttable is the one outcome worse than
        not refining.
        """
        layouts, unplaced = fill_finite_bins_max_yield(
            pool,
            specs,
            cutting_params=cutting_params,
            budget=budget,
            seed=seed,
            min_rect_size=min_rect_size,
            max_sheets=max_sheets,
            exact_config=exact_config,
        )
        cut_ids = {pp.piece.id for layout in layouts for pp in layout.placed_pieces}
        kept = [piece for piece in pool if piece.id in cut_ids]
        if not kept:
            return layouts, unplaced
        repacked, spilled = run(kept)
        if spilled:
            return layouts, unplaced
        if finite_plan_objective(repacked, unplaced) < finite_plan_objective(
            layouts, unplaced
        ):
            return repacked, unplaced
        return layouts, unplaced

    layouts, unplaced = plan(pieces)
    by_id = {piece.id: piece for piece in pieces}
    rotatable = any(piece.can_rotate for piece in pieces)

    def consider(candidate: Tuple[List[CuttingLayout], List[Piece]]) -> None:
        """Adopts ``candidate`` only if it scores strictly better."""
        nonlocal layouts, unplaced
        alt_layouts, alt_unplaced = candidate
        if finite_plan_objective(alt_layouts, alt_unplaced) < finite_plan_objective(
            layouts, unplaced
        ):
            layouts, unplaced = alt_layouts, alt_unplaced

    # The unrotated candidate. Gated on the symptom rather than computed always:
    # a plan that turned nothing has no complaint to answer, and a pool with no
    # rotatable piece is its own clone. Cheap where it matters -- a finite pool is
    # "the client walked in with two retazos", the smallest job the shop runs.
    if rotatable and (unplaced or _plan_has_rotation(layouts)):
        consider(_rebind_to_original(*plan(_without_rotation(pieces)), by_id))

    # Still short of material: ask the yield question outright, in both
    # orientations. ``optimize_bins`` cannot answer it -- it only ranks plans
    # that place everything, and it skips its own search the moment the greedy
    # baseline strands a piece, so the plan above came out of a single greedy
    # pass. This is what the note that used to sit here deferred: which pieces
    # are left over stops being a greedy accident.
    if unplaced:
        consider(max_yield(pieces))
        if rotatable:
            consider(_rebind_to_original(*max_yield(_without_rotation(pieces)), by_id))

    return layouts, unplaced
