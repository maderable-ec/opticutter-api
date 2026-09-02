"""Re-derives a board's guillotine cut tree from its FINAL piece positions.

The packer decides *where* the pieces go and, as a side effect of how it splits
free rectangles, also decides *in which order the saw runs*. Those are two
different questions and only the first one is optimized. ``_split_remainder``
picks its split rule per placement, looking at the local leftover with no notion
of the rest of the board, and ``_cuts_for`` then emits whatever cut that
particular split implies. The result is a valid guillotine pattern, but rarely
the one that leaves the fewest usable offcuts.

The canonical example (pre-order 4, six 320x2500 strips on a 2070x2800 sheet):
``SHORTER_LEFTOVER_AXIS`` rips six full-height columns and only then crops each
one to 2500, leaving five identical 320x276 scraps plus a right-hand strip —
seven leftovers. The very same placements also admit *one* crosscut at y=2510
followed by six rips, which leaves exactly two: a 2050x276 band and a 106x2500
strip. Same pieces, same board, same bill; a different tree.

So this module chooses. Given the placements as immutable input, it searches the
guillotine trees that realize them and keeps the one that consolidates leftovers
best. **Nothing here can change the board count, the cost or the efficiency** —
it never moves a piece, and ``CuttingLayout.waste_area`` is computed from the
pieces rather than from this list. That is the whole point: the shop wanted
bigger offcuts without paying a board for them.

Kept framework-free (like the rest of ``src/cutting/``) and deliberately in
Python: measured at ~1.5 ms for a typical board and 74 ms for the worst dense
one (109 pieces), it is ~1% of the engine, where the packing kernel was ~90%
before its Rust port. Should the node budget ever have to grow by orders of
magnitude, this is shaped to transliterate the same way ``packer.py`` was.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting.models import Cut, CuttingLayout, Rectangle
from src.cutting.parameters import CuttingParameters

_EPS = 1e-6

# Below this, a leftover is scrap rather than an offcut the shop would rack. It
# only ever enters the *objective* (never the reported list), and it is what
# stops consolidation from trading usable material for one prettier rectangle:
# scoring on rectangle sizes alone grows the biggest leftover by 14.6% while
# losing 4.4% of the usable area, because it happily shaves a second usable
# offcut into slivers. Measured on 221 boards, not assumed.
DEFAULT_MIN_USABLE_OFFCUT = 150.0

# Regions visited before the search gives up and the caller keeps the packer's
# own tree. Counted in nodes and never in wall clock, like every other stopping
# rule in this engine, so the answer stays deterministic and cacheable. ~5x the
# worst case measured (3799 nodes on a 109-piece board).
DEFAULT_NODE_BUDGET = 20_000

# (x0, y0, x1, y1) of a region, plus the pieces inside it.
_Region = Tuple[float, float, float, float, Tuple[int, ...]]
# (cuts, leftovers, total cut length) for one region.
_Plan = Tuple[List[Cut], List[Rectangle], float]
# Lexicographic, lower is better. See ``_score``.
_Score = Tuple[float, float, int, float]


def _score(rects: Sequence[Rectangle], cut_length: float, min_usable: float) -> _Score:
    """Ranks a set of leftovers, lower first.

    Lexicographic and, crucially, **additive in every component**, which is what
    makes the memoized recursion correct: a region's siblings contribute a fixed
    amount, so minimizing a child's tuple minimizes the parent's total.

    1. usable area, negated — never forfeit material the shop can still cut from;
    2. sum of squared usable areas, negated — consolidate it into fewer, bigger
       pieces (a superlinear reward, so one 0.6m2 beats two 0.3m2);
    3. leftover count — break remaining ties towards a tidier board;
    4. cut length — and then towards less saw travel.
    """
    usable_area = 0.0
    squares = 0.0
    for rect in rects:
        if rect.width >= min_usable and rect.height >= min_usable:
            # In square metres: mm^2 squared overflows into numbers whose
            # rounding is meaningless.
            area = rect.area / 1e6
            usable_area += area
            squares += area * area
    return (
        -round(usable_area, 6),
        -round(squares, 9),
        len(rects),
        round(cut_length, 3),
    )


class _Consolidator:
    """One board's re-derivation. Single-use: it carries the node budget."""

    def __init__(
        self,
        boxes: Sequence[Tuple[float, float, float, float]],
        kerf: float,
        min_rect_size: float,
        min_usable: float,
        node_budget: int,
    ):
        self.boxes = boxes
        self.kerf = kerf
        self.min_rect_size = min_rect_size
        self.min_usable = min_usable
        self.budget = node_budget
        self.memo: Dict[_Region, Optional[_Plan]] = {}

    def solve(
        self, x0: float, y0: float, x1: float, y1: float, idxs: Tuple[int, ...]
    ) -> Optional[_Plan]:
        """Best tree for the region, or ``None`` if there is none (or no budget).

        ``None`` is not "impossible to cut": the packer's own tree is always a
        witness that one exists. It means this model could not express one, and
        the caller then keeps what the packer emitted.
        """
        key = (x0, y0, x1, y1, idxs)
        cached = self.memo.get(key, False)
        if cached is not False:
            return cached  # type: ignore[return-value]

        self.budget -= 1
        if self.budget < 0:
            return None

        width = x1 - x0
        height = y1 - y0

        if not idxs:
            # Nothing left to free: the region *is* the offcut.
            keep = width >= self.min_rect_size and height >= self.min_rect_size
            plan: _Plan = ([], [Rectangle(x0, y0, width, height)] if keep else [], 0.0)
            self.memo[key] = plan
            return plan

        if len(idxs) == 1:
            px0, py0, px1, py1 = self.boxes[idxs[0]]
            if (
                abs(px0 - x0) < _EPS
                and abs(py0 - y0) < _EPS
                and abs(px1 - x1) < _EPS
                and abs(py1 - y1) < _EPS
            ):
                # The piece already is the region: it came free with the cut
                # that carved this region out.
                self.memo[key] = ([], [], 0.0)
                return self.memo[key]

        best_score: Optional[_Score] = None
        best_plan: Optional[_Plan] = None

        for horizontal, position in self._candidates(x0, y0, x1, y1, idxs):
            plan = self._apply(x0, y0, x1, y1, idxs, horizontal, position)
            if plan is None:
                if self.budget < 0:
                    return None
                continue
            cuts, rects, cut_length = plan
            # ``(position, horizontal)`` last so equal-scoring trees resolve the
            # same way on every machine and every run.
            score = _score(rects, cut_length, self.min_usable) + (position, horizontal)
            if best_score is None or score < best_score:
                best_score = score
                best_plan = plan

        self.memo[key] = best_plan
        return best_plan

    def _candidates(
        self, x0: float, y0: float, x1: float, y1: float, idxs: Tuple[int, ...]
    ) -> List[Tuple[bool, float]]:
        """Guillotine cuts that separate the region without touching a piece.

        Only two positions per piece and axis are ever worth trying: flush past
        its far edge, or flush before its near edge (``edge - kerf``). Anything
        strictly between them yields the same partition with a smaller leftover.

        A cut may overhang the region's far boundary (``position + kerf > x1``).
        That is what covers slack narrower than the blade, which the packer
        creates whenever a leftover was under one kerf: no cut fits *inside* it,
        yet the piece still has to be freed. Without this, 23% of real boards
        came back as "no tree exists".
        """
        xs = set()
        ys = set()
        for i in idxs:
            px0, py0, px1, py1 = self.boxes[i]
            for x in (px1, px0 - self.kerf):
                if x0 + _EPS < x < x1 - _EPS:
                    xs.add(round(x, 6))
            for y in (py1, py0 - self.kerf):
                if y0 + _EPS < y < y1 - _EPS:
                    ys.add(round(y, 6))

        blade = self.kerf - _EPS
        candidates: List[Tuple[bool, float]] = []
        for x in sorted(xs):
            if all(
                self.boxes[i][0] >= x + blade or self.boxes[i][2] <= x + _EPS
                for i in idxs
            ):
                candidates.append((False, x))
        for y in sorted(ys):
            if all(
                self.boxes[i][1] >= y + blade or self.boxes[i][3] <= y + _EPS
                for i in idxs
            ):
                candidates.append((True, y))
        return candidates

    def _apply(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        idxs: Tuple[int, ...],
        horizontal: bool,
        position: float,
    ) -> Optional[_Plan]:
        """Splits the region at the cut and solves both sides."""
        blade = self.kerf - _EPS
        if horizontal:
            near = tuple(i for i in idxs if self.boxes[i][3] <= position + _EPS)
            far = tuple(i for i in idxs if self.boxes[i][1] >= position + blade)
            cut = Cut(x0, position, x1 - x0, is_horizontal=True)
            bounds = (
                (x0, y0, x1, position),
                (x0, min(position + self.kerf, y1), x1, y1),
            )
        else:
            near = tuple(i for i in idxs if self.boxes[i][2] <= position + _EPS)
            far = tuple(i for i in idxs if self.boxes[i][0] >= position + blade)
            cut = Cut(position, y0, y1 - y0, is_horizontal=False)
            bounds = (
                (x0, y0, position, y1),
                (min(position + self.kerf, x1), y0, x1, y1),
            )

        if len(near) + len(far) != len(idxs):
            # A piece sits in the blade's path. ``_candidates`` already excluded
            # this, but the two tests must agree or a piece would vanish.
            return None

        near_plan = self.solve(*bounds[0], near)
        if near_plan is None:
            return None
        # The blade can consume the far side entirely (slack under one kerf).
        far_bounds = bounds[1]
        if (
            far_bounds[0] < far_bounds[2] - _EPS
            and far_bounds[1] < far_bounds[3] - _EPS
        ):
            far_plan = self.solve(*far_bounds, far)
            if far_plan is None:
                return None
        elif far:
            return None
        else:
            far_plan = ([], [], 0.0)

        return (
            [cut] + near_plan[0] + far_plan[0],
            near_plan[1] + far_plan[1],
            cut.length + near_plan[2] + far_plan[2],
        )


def consolidate_layout(
    layout: CuttingLayout,
    params: CuttingParameters,
    *,
    min_rect_size: float = 0.1,
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> CuttingLayout:
    """Returns ``layout`` with the best cut tree its placements admit.

    Never worse: the packer's own tree competes under the same score and wins
    ties, so a board the search cannot improve (or cannot decompose at all) comes
    back untouched. ``placed_pieces`` and ``material`` are always the originals —
    only ``cuts`` and ``remainders`` may change, and the cuts come out in tree
    order, which is the order the operator runs the saw.
    """
    pieces = layout.placed_pieces
    if not pieces:
        return layout

    material = layout.material
    x0 = params.left_trim
    y0 = params.bottom_trim
    x1 = material.width - params.right_trim
    y1 = material.height - params.top_trim
    if x1 - x0 < min_rect_size or y1 - y0 < min_rect_size:
        return layout

    # Sorted so the region keys — and therefore the memo hits and the tie-breaks
    # — depend on the geometry alone, never on placement order.
    boxes = sorted((p.x, p.y, p.x + p.width, p.y + p.height) for p in pieces)
    consolidator = _Consolidator(
        boxes=boxes,
        kerf=max(0.0, params.kerf),
        min_rect_size=max(0.01, min_rect_size),
        min_usable=min_usable_offcut,
        node_budget=node_budget,
    )
    plan = consolidator.solve(x0, y0, x1, y1, tuple(range(len(boxes))))
    if plan is None:
        return layout

    cuts, rects, cut_length = plan
    if _score(rects, cut_length, min_usable_offcut) >= _score(
        layout.remainders, layout.cut_length, min_usable_offcut
    ):
        return layout

    return CuttingLayout(
        material=material,
        placed_pieces=pieces,
        remainders=rects,
        sheet_number=layout.sheet_number,
        cuts=cuts,
    )


def consolidate_layouts(
    layouts: List[CuttingLayout],
    params: CuttingParameters,
    *,
    min_rect_size: float = 0.1,
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT,
) -> List[CuttingLayout]:
    """``consolidate_layout`` over a finished plan."""
    return [
        consolidate_layout(
            layout,
            params,
            min_rect_size=min_rect_size,
            min_usable_offcut=min_usable_offcut,
        )
        for layout in layouts
    ]
