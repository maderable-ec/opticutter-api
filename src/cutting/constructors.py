"""Candidate bin-fill generators for the board-count search.

Each generator packs ONE bin from a pool of remaining pieces and returns a
``BinFill`` (placements + leftovers + cuts + a dedupe signature). The search
layer (``search.py``) evaluates many candidates per board and keeps the ones
that lead to the cheapest global solution — no single generator is trusted:
the audit showed every greedy fails at its own scale.

Two families:

- ``greedy_fill``: the classic one-pass packer under a ``GreedyConfig``
  (piece order x free-rect selection x split rule). Cheap and strong on
  homogeneous jobs.
- ``strip_fill``: builds the bin as first-stage guillotine strips (the
  structure commercial optimizers emit): candidate strip sizes come from the
  remaining piece dimensions plus the bin's residual width, and each strip is
  packed recursively with the guillotine packer, which yields 2/3-stage
  patterns (sub-columns inside strips). ``first_dim`` seeds diversity: forcing
  the first strip lets the search explore layouts a pure efficiency-greedy
  would never open with (e.g. distributing full-length thin strips across
  boards instead of hoarding them into one).
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.cutting.enums import PackingStrategy, SplitRule
from src.cutting.models import BinSpec, Cut, Material, Piece, PlacedPiece, Rectangle
from src.cutting.packer import GuillotineOptimizer
from src.cutting.parameters import CuttingParameters

# Piece-type identity: geometry + constraints. Two pieces of the same type are
# interchangeable for the search (ids only matter for downstream attribution).
PieceType = Tuple[float, float, bool, int]


def piece_type(piece: Piece) -> PieceType:
    return (piece.width, piece.height, piece.can_rotate, piece.priority)


SORT_KEYS: Dict[str, Callable[[Piece], tuple]] = {
    "area": lambda p: (-p.priority, -p.area, -p.height, p.id),
    "maxdim": lambda p: (
        -p.priority,
        -max(p.width, p.height),
        -min(p.width, p.height),
        p.id,
    ),
    "height": lambda p: (-p.priority, -p.height, -p.width, p.id),
    "width": lambda p: (-p.priority, -p.width, -p.height, p.id),
    "perimeter": lambda p: (-p.priority, -(p.width + p.height), -p.area, p.id),
    "mindim": lambda p: (
        -p.priority,
        -min(p.width, p.height),
        -max(p.width, p.height),
        p.id,
    ),
}


@dataclass(frozen=True)
class GreedyConfig:
    """One point of the greedy portfolio: order x selection x split."""

    sort: str
    split: SplitRule
    # ``MAX_EFFICIENCY`` selects free rects by Best-Area-Fit, ``LONG_OFFCUTS``
    # by Bottom-Left; reused here as the selection axis of the portfolio.
    selection: PackingStrategy = PackingStrategy.MAX_EFFICIENCY


# Deterministic portfolio order. Curated: the full cartesian product mostly
# yields duplicate fills (dedupe absorbs them) but costs decodes; this set
# covers the distinct behaviors observed in the audit experiments.
GREEDY_PORTFOLIO: Tuple[GreedyConfig, ...] = tuple(
    GreedyConfig(sort=s, split=sp, selection=sel)
    for s in ("area", "maxdim", "height", "width", "perimeter", "mindim")
    for sp in (
        SplitRule.SHORTER_LEFTOVER_AXIS,
        SplitRule.LONGER_AXIS,
        SplitRule.MINIMIZE_AREA,
        SplitRule.MAXIMIZE_AREA,
    )
    for sel in (PackingStrategy.MAX_EFFICIENCY, PackingStrategy.LONG_OFFCUTS)
)


@dataclass
class BinFill:
    """One candidate fill of one bin."""

    spec: BinSpec
    placed: List[PlacedPiece]
    remainders: List[Rectangle]
    cuts: List[Cut]

    @property
    def used_area(self) -> float:
        return sum(p.width * p.height for p in self.placed)

    @property
    def waste_area(self) -> float:
        return self.spec.area - self.used_area

    def type_signature(self) -> Tuple[Tuple[PieceType, int], ...]:
        """Multiset of piece types placed — the dedupe identity of a fill."""
        counts: Dict[PieceType, int] = {}
        for pp in self.placed:
            t = piece_type(pp.piece)
            counts[t] = counts.get(t, 0) + 1
        return tuple(sorted(counts.items()))

    def placed_ids(self) -> List[str]:
        return [pp.piece.id for pp in self.placed]


def greedy_fill(
    pool: Sequence[Piece],
    spec: BinSpec,
    cutting_params: CuttingParameters,
    config: GreedyConfig,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """One-pass greedy fill of ``spec`` under ``config``; ``None`` if nothing fits."""
    try:
        packer = GuillotineOptimizer(
            material=spec.to_material(),
            cutting_params=cutting_params,
            split_rule=config.split,
            strategy=config.selection,
            min_rect_size=min_rect_size,
        )
    except ValueError:
        # Trims exceed the bin dimensions (tiny offcut): unusable.
        return None
    ordered = sorted(pool, key=SORT_KEYS[config.sort])
    placed, _ = packer.optimize(ordered, presorted=True)
    if not placed:
        return None
    return BinFill(
        spec=spec,
        placed=placed,
        remainders=packer.remainders,
        cuts=packer.cuts,
    )


def _strip_candidate_dims(
    pool: Sequence[Piece], limit: float, across: bool
) -> List[float]:
    """Distinct piece dimensions usable as strip sizes, descending.

    ``across=False`` collects the dimension perpendicular to the strip's long
    axis for vertical strips (piece widths, plus heights of rotatable pieces);
    ``across=True`` the same for horizontal strips.
    """
    dims = set()
    for p in pool:
        primary = p.height if across else p.width
        secondary = p.width if across else p.height
        if primary <= limit:
            dims.add(primary)
        if p.can_rotate and secondary <= limit:
            dims.add(secondary)
    return sorted(dims, reverse=True)


def _strip_material(spec: BinSpec, width: float, height: float) -> Material:
    """Strip-sized pseudo material for the nested packer (id irrelevant)."""
    return Material(
        id=spec.key,
        width=width,
        height=height,
        thickness=spec.thickness,
        cost_per_unit=0.0,
    )


def _translate(
    placed: List[PlacedPiece],
    cuts: List[Cut],
    remainders: List[Rectangle],
    dx: float,
    dy: float,
) -> Tuple[List[PlacedPiece], List[Cut], List[Rectangle]]:
    moved_pieces = [
        PlacedPiece(
            piece=pp.piece,
            x=pp.x + dx,
            y=pp.y + dy,
            width=pp.width,
            height=pp.height,
            rotated=pp.rotated,
        )
        for pp in placed
    ]
    moved_cuts = [
        Cut(c.x + dx, c.y + dy, c.length, is_horizontal=c.is_horizontal) for c in cuts
    ]
    moved_rects = [Rectangle(r.x + dx, r.y + dy, r.width, r.height) for r in remainders]
    return moved_pieces, moved_cuts, moved_rects


def strip_fill(
    pool: Sequence[Piece],
    spec: BinSpec,
    cutting_params: CuttingParameters,
    horizontal: bool = False,
    first_dim: Optional[float] = None,
    max_repeat: Optional[int] = None,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """Fills ``spec`` as a sequence of first-stage guillotine strips.

    Vertical strips span the usable height and consume width (a table-saw rip
    along the long axis); ``horizontal=True`` mirrors the roles (the first
    stage is a crosscut). Each strip is a nested recursive guillotine pack, so
    strips naturally contain sub-columns (3-stage patterns). Strip sizes are
    chosen by packed efficiency among the remaining piece dimensions plus the
    bin residual, which lets narrow leftovers host paired sub-columns.

    ``max_repeat`` caps how many strips with an identical piece-type content
    one board commits. A perfect single-type strip (e.g. a full-length 70mm
    rail, 100% efficient) greedily hoards every sibling into one board — the
    exact move that strands the rest of the job; capping repeats yields the
    candidates that SPREAD those strips across boards, and the search decides
    which distribution wins.
    """
    params = cutting_params or CuttingParameters()
    kerf = max(0, params.kerf)
    left, right = max(0, params.left_trim), max(0, params.right_trim)
    top, bottom = max(0, params.top_trim), max(0, params.bottom_trim)
    usable_w = spec.width - left - right
    usable_h = spec.height - top - bottom
    if usable_w <= 0 or usable_h <= 0:
        return None

    # Work in strip-local coordinates: strips advance along ``axis_room``.
    axis_room = usable_h if horizontal else usable_w
    strip_span = usable_w if horizontal else usable_h

    remaining = list(pool)
    all_placed: List[PlacedPiece] = []
    all_cuts: List[Cut] = []
    all_rects: List[Rectangle] = []
    cursor = 0.0
    room = axis_room
    first = True
    repeat_counts: Dict[Tuple[Tuple[PieceType, int], ...], int] = {}

    strip_params = CuttingParameters(kerf=kerf)

    while remaining and room >= min_rect_size:
        dims = _strip_candidate_dims(remaining, room, across=horizontal)
        if not dims:
            break
        candidates = list(dims)
        if room not in candidates:
            candidates.append(room)
        if first and first_dim is not None:
            if first_dim <= room:
                candidates = [first_dim]
            first = False

        best = None
        for size in candidates:
            if horizontal:
                mat_w, mat_h = strip_span, size
            else:
                mat_w, mat_h = size, strip_span
            try:
                packer = GuillotineOptimizer(
                    material=_strip_material(spec, mat_w, mat_h),
                    cutting_params=strip_params,
                    min_rect_size=min_rect_size,
                )
            except ValueError:
                continue
            fitting = [
                p
                for p in remaining
                if (p.width <= mat_w and p.height <= mat_h)
                or (p.can_rotate and p.height <= mat_w and p.width <= mat_h)
            ]
            if not fitting:
                continue

            # Widest-fit-first inside the strip (rotation-aware) wastes the
            # least strip size; the nested packer still nests narrower
            # sub-columns recursively.
            def _fit_across(p: Piece, limit: float, across_height: bool) -> float:
                natural = p.height if across_height else p.width
                rotated = p.width if across_height else p.height
                dims = []
                if natural <= limit:
                    dims.append(natural)
                if p.can_rotate and rotated <= limit:
                    dims.append(rotated)
                return max(dims) if dims else 0.0

            if horizontal:
                fitting.sort(
                    key=lambda p: (
                        -p.priority,
                        -_fit_across(p, mat_h, True),
                        -p.width,
                        p.id,
                    )
                )
            else:
                fitting.sort(
                    key=lambda p: (
                        -p.priority,
                        -_fit_across(p, mat_w, False),
                        -p.height,
                        p.id,
                    )
                )
            placed, _ = packer.optimize(fitting, presorted=True)
            if not placed:
                continue
            if max_repeat is not None:
                sig_counts: Dict[PieceType, int] = {}
                for pp in placed:
                    t = piece_type(pp.piece)
                    sig_counts[t] = sig_counts.get(t, 0) + 1
                strip_sig = tuple(sorted(sig_counts.items()))
                if repeat_counts.get(strip_sig, 0) >= max_repeat:
                    continue
            else:
                strip_sig = None
            used = sum(pp.width * pp.height for pp in placed)
            eff = used / (mat_w * mat_h)
            score = (eff, used, -size)
            if best is None or score > best[0]:
                best = (score, size, placed, packer, strip_sig)
        if best is None:
            break

        _, size, placed, packer, strip_sig = best
        if strip_sig is not None:
            repeat_counts[strip_sig] = repeat_counts.get(strip_sig, 0) + 1
        if horizontal:
            dx, dy = left, bottom + cursor
        else:
            dx, dy = left + cursor, bottom
        moved_pieces, moved_cuts, moved_rects = _translate(
            placed, packer.cuts, packer.remainders, dx, dy
        )
        all_placed.extend(moved_pieces)
        all_cuts.extend(moved_cuts)
        all_rects.extend(moved_rects)

        leftover = room - size
        if leftover > 0:
            # First-stage cut separating this strip from the rest of the bin.
            if horizontal:
                all_cuts.append(
                    Cut(left, bottom + cursor + size, usable_w, is_horizontal=True)
                )
            else:
                all_cuts.append(
                    Cut(left + cursor + size, bottom, usable_h, is_horizontal=False)
                )
            advance = size + kerf
        else:
            advance = size
        cursor += advance
        room -= advance

        placed_ids = {pp.piece.id for pp in placed}
        remaining = [p for p in remaining if p.id not in placed_ids]

    if not all_placed:
        return None

    if room >= min_rect_size:
        # Unopened tail of the bin: one continuous first-stage leftover.
        if horizontal:
            all_rects.append(Rectangle(left, bottom + cursor, usable_w, room))
        else:
            all_rects.append(Rectangle(left + cursor, bottom, room, usable_h))

    return BinFill(spec=spec, placed=all_placed, remainders=all_rects, cuts=all_cuts)
