"""Multi-material pool packing: a catalog board + its attached (finite) offcuts.

A *pool* lets one group of pieces be cut across a catalog board (infinite supply)
and one or more client/company offcuts of the same material (finite supply). It
reuses the pure cutting engine — ``GuillotineOptimizer`` for each finite offcut
sheet and ``MultiSheetGuillotineOptimizer`` for the catalog — so every resulting
layout stays single-material and flows unchanged through billing, persistence and
the proforma.

Three fill orders (see ``PoolFillOrder``):
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
) -> List[CuttingLayout]:
    offcut_layouts, remaining = _fill_offcuts(
        offcuts, pieces, cutting_params, strategy, min_rect_size
    )
    catalog_layouts, _ = _fill_catalog(
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
    return offcut_layouts + catalog_layouts


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
) -> List[CuttingLayout]:
    """Fewest catalog boards such that the offcuts absorb the residual.

    ``Nc`` = boards needed catalog-only (upper bound). We look for the smallest
    ``k`` in ``0..Nc`` where ``k`` catalog boards + the offcuts place every piece,
    so the residual (the tail that would otherwise sit on a bought board) lands on
    the client's offcuts. ``k = Nc`` always works (catalog alone fits all), so a
    solution is guaranteed.
    """
    catalog_only, _ = _fill_catalog(
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
            return catalog_layouts + offcut_layouts

    # Unreachable (k = nc places everything on catalog); guard for safety.
    return catalog_only


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
) -> List[CuttingLayout]:
    """Packs ``pieces`` across the catalog board + its finite offcuts.

    Returns the combined single-material layouts (offcut sheets + catalog sheets).
    The fill order comes from ``primary.fill_order``; ``auto`` keeps whichever of
    ``offcuts_first``/``catalog_first`` wastes least catalog area (deterministic).
    ``half_spec`` (catalog materials only) enables the final half-board
    downgrade on the purchased sheets.
    """
    if not pieces:
        return []
    if not offcuts:
        layouts, _ = _fill_catalog(
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
        return _apply_half_downgrade(
            layouts, half_spec, cutting_params, seed, min_rect_size, exact_config
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
        layouts = _offcuts_first(*args)
    elif order == PoolFillOrder.catalog_first:
        layouts = _catalog_first(*args)
    else:
        candidates = [_offcuts_first(*args), _catalog_first(*args)]
        layouts = min(candidates, key=lambda ls: _catalog_waste_score(ls, primary.key))
    return _apply_half_downgrade(
        layouts, half_spec, cutting_params, seed, min_rect_size, exact_config
    )
