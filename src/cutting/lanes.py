"""A complete-plan candidate built from full-height lanes.

Why this exists
---------------
The beam searches partitions one sheet at a time: it fills a board, then asks what
to do with the rest. That is the right shape for most cut lists, but it cannot
see a global width argument — and for furniture cut lists the partition IS a
width argument. Pre-order 132 (54 pieces, 12.7646 m² of MDP 2.80x2.07) is the
case that motivated this module: the engine billed **3 whole boards** where
**2 + a half** provably fit, and the half is the cheaper sheet the shop already
sells.

The observation is that a furniture cut list is mostly made of pieces that share
a width (sides, dividers, shelves). Stack those into full-height *lanes* and the
whole plan collapses to a 1-D question — which lanes go on which sheet — that is
small enough to solve exactly. What is left over (the low, wide pieces: rails,
toe-kicks) rides in the strip above the lanes.

The piece that decides pre-order 132
------------------------------------
A rotatable piece can stand in a lane or lie down in a strip, and the choice is
worth a whole lane. Its 6 pieces of 622x222 arrive lying down; standing all six
needs 17 lanes and 3 whole boards, while leaving **two** of them lying down needs
16 and fits 2 boards + a half. So the orientation split is enumerated rather than
fixed — a constructor that only ever sends the *low* pieces to the strips never
finds this.

What it is not
--------------
It is a candidate, not an authority: ``lane_plan`` returns a complete plan that
bills strictly less than the incumbent, or ``None``. It never enters the beam, so
it cannot displace a correct partition the way an extra cheap bin can
(``search.py`` documents that hazard on the half board). Everything here is pure
Python and budgeted in nodes and candidates, never wall clock, so the answer is
reproducible and the payload cache stays valid.

Deliberate limits, each one a ``None`` rather than a worse plan:
- only bins of unlimited supply take part (a finite offcut pool is ``pool.py``'s
  job, and its supply constraint is not a 1-D width question);
- only bins that share the tallest usable height, since a lane is full-height by
  definition — a board halved across its short side (plywood, ranurado) has no
  half sibling of the same height and simply gets no candidate;
- at most two bin classes (the whole sheet and its half), which is every catalog
  pool the shop quotes.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.cutting.constructors import BinFill, _translate
from src.cutting.enums import Selection, SplitRule
from src.cutting.models import BinSpec, Cut, Piece, PlacedPiece, Rectangle
from src.cutting.packer import GuillotineOptimizer
from src.cutting.parameters import CuttingParameters
from src.cutting.sheet_check import is_cuttable

# How many rotatable pieces may be pulled out of the lanes and laid down in the
# strips. Counted in candidates, never in time: each value of ``g`` is one full
# re-plan, and the loop runs ``g = 0..LAY_DOWN_CANDIDATES``. Pre-order 132 needs
# g=2; the cap is set well above that because the pass costs microseconds and
# the enumeration is the whole point of the module.
LAY_DOWN_CANDIDATES = 12

# How many width groups may be pulled out of the lanes entirely and sent to the
# strips, worst lane fill first. Also candidates, not time.
STRIP_GROUP_CANDIDATES = 6

# Node ceiling for ONE feasibility probe of the 1-D width packing. Low on
# purpose: the assignments that matter are found by the first-fit pass below
# without opening the tree at all, and what the tree is left doing is PROVING a
# combination infeasible -- which is the expensive half and the one worth
# cutting short. Giving up only costs a candidate, never a wrong plan.
_PACK_NODE_BUDGET = 2000

# Node ceiling for a whole ``lane_plan`` call, shared across every candidate and
# every sheet combination it tries. The per-probe ceiling alone is not enough: a
# call enumerates strip groups x lay-downs x combinations, and on a pool of
# offcuts that product was measured at 7-9 seconds for a 20-piece pool. Spending
# it is also pointless there, since a pool whose sheets are offcuts is not a
# width argument. One shared budget bounds the worst case outright, and running
# out only costs the candidate.
_TOTAL_NODE_BUDGET = 60000

# Most sheets a candidate may open. Past this the 1-D enumeration is not the
# bottleneck the job has.
_MAX_SHEETS_TRIED = 40

_EPS = 1e-9


@dataclass
class _Item:
    """One piece in a chosen orientation."""

    piece: Piece
    width: float
    height: float
    rotated: bool


@dataclass
class _Lane:
    """A full-height column of pieces that share a width."""

    width: float
    items: List[_Item]
    height: float  # what the stack actually uses, kerfs included


@dataclass
class _Sheet:
    spec: BinSpec
    lanes: List[_Lane]


def _usable(spec: BinSpec, params: CuttingParameters) -> Tuple[float, float]:
    w = spec.width - max(0.0, params.left_trim) - max(0.0, params.right_trim)
    h = spec.height - max(0.0, params.top_trim) - max(0.0, params.bottom_trim)
    return max(0.0, w), max(0.0, h)


def _orientations(piece: Piece) -> List[Tuple[float, float, bool]]:
    """(width, height, rotated) options, narrow side standing first.

    Standing a piece on its narrow side is what makes lanes cheap, so that
    orientation leads; the rotated flag follows the convention of the rest of
    the engine (``rotated`` means width and height were swapped).
    """
    if not piece.can_rotate or piece.width == piece.height:
        return [(piece.width, piece.height, False)]
    if piece.width <= piece.height:
        return [(piece.width, piece.height, False), (piece.height, piece.width, True)]
    return [(piece.height, piece.width, True), (piece.width, piece.height, False)]


def _stack(items: Sequence[_Item], height: float, kerf: float) -> List[_Lane]:
    """First-fit-decreasing of equal-width items into full-height lanes.

    Deterministic by construction: the items arrive in a stable order and the
    sort key ends in the piece id, so two runs build the same lanes.
    """
    lanes: List[_Lane] = []
    for item in sorted(items, key=lambda it: (-it.height, it.piece.id)):
        if item.height > height + _EPS:
            # The caller classified a piece that cannot stand; no plan, rather
            # than a plan missing a piece.
            return []
        for lane in lanes:
            used = lane.height + kerf + item.height
            if used <= height + _EPS:
                lane.items.append(item)
                lane.height = used
                break
        else:
            lanes.append(_Lane(width=item.width, items=[item], height=item.height))
    return lanes


def _pack_widths(
    widths: Sequence[float],
    classes: Sequence[Tuple[BinSpec, float]],
    kerf: float,
    below_cost: float,
    max_sheets: int,
    budget: List[int],
) -> Optional[List[Tuple[BinSpec, List[int]]]]:
    """Cheapest assignment of lane widths to sheets, or ``None``.

    ``classes`` is ``(spec, usable_width)`` per bin class, all of unlimited
    supply. Combinations are enumerated in increasing cost, so the first
    feasible one is optimal — and anything at or above ``below_cost`` is skipped
    outright, which is what keeps this from ever proposing a worse plan.
    """
    if not widths:
        return None
    order = sorted(range(len(widths)), key=lambda i: (-widths[i], i))
    combos: List[Tuple[float, Tuple[int, ...]]] = []
    # ``below_cost`` bounds how many sheets of each class are worth enumerating
    # at all: more than that and the combination is already too expensive to
    # adopt. On pre-order 132 it is the difference between 18 combinations and
    # 1681. A free bin gets no such bound, hence the fallback.
    counts = []
    for spec, _width in classes:
        limit = max_sheets
        if spec.cost_per_unit > 0:
            limit = min(limit, int(below_cost / spec.cost_per_unit))
        counts.append(range(0, limit + 1))
    for combo in _product(counts):
        n = sum(combo)
        if n == 0 or n > max_sheets:
            continue
        cost = sum(c * cls[0].cost_per_unit for c, cls in zip(combo, classes))
        if cost >= below_cost - _EPS:
            continue
        combos.append((cost, combo))
    combos.sort(key=lambda c: (c[0], c[1]))

    total = sum(widths)
    for _cost, combo in combos:
        caps: List[Tuple[BinSpec, float]] = []
        for count, (spec, usable_w) in zip(combo, classes):
            caps.extend([(spec, usable_w)] * count)
        # Width alone rules most combinations out, and it costs one sum against
        # a backtracking search that would otherwise walk the whole tree to say
        # the same thing.
        if total > sum(width for _spec, width in caps) + _EPS:
            continue
        # Widest sheet first: a lane that only fits the big sheet must be seen
        # while that sheet is still empty.
        caps.sort(key=lambda c: (-c[1], c[0].cost_per_unit))
        if budget[0] <= 0:
            return None
        assigned = _assign(order, widths, [c[1] for c in caps], kerf, budget)
        if assigned is not None:
            return [(caps[i][0], assigned[i]) for i in range(len(caps))]
    return None


def _product(ranges: Sequence[range]) -> List[Tuple[int, ...]]:
    out: List[Tuple[int, ...]] = [()]
    for r in ranges:
        out = [prev + (value,) for prev in out for value in r]
    return out


def _first_fit(
    order: Sequence[int],
    widths: Sequence[float],
    caps: Sequence[float],
    kerf: float,
) -> Optional[List[List[int]]]:
    """Widest lane into the first sheet that still holds it.

    Cheap, and it happens to be what a cutting plan looks like when it works:
    pre-order 132's winning assignment -- three 620s on one sheet, nine 222s on
    the next, the rest on the half -- is exactly first fit on widths.
    """
    used = [0.0] * len(caps)
    bins: List[List[int]] = [[] for _ in caps]
    for idx in order:
        width = widths[idx]
        for j in range(len(caps)):
            need = width + (kerf if used[j] > 0 else 0.0)
            if used[j] + need <= caps[j] + _EPS:
                used[j] += need
                bins[j].append(idx)
                break
        else:
            return None
    return bins


def _assign(
    order: Sequence[int],
    widths: Sequence[float],
    caps: Sequence[float],
    kerf: float,
    budget: List[int],
) -> Optional[List[List[int]]]:
    """Bin packing of lane widths: first fit, then an exact tree if it fails."""
    quick = _first_fit(order, widths, caps, kerf)
    if quick is not None:
        return quick
    used = [0.0] * len(caps)
    bins: List[List[int]] = [[] for _ in caps]
    nodes = [0]
    # Width still to place from step k on. The free-width bound below is what
    # keeps the search from walking a subtree that cannot possibly close.
    pending = [0.0] * (len(order) + 1)
    for k in range(len(order) - 1, -1, -1):
        pending[k] = pending[k + 1] + widths[order[k]]
    free = sum(caps)

    def place(k: int) -> bool:
        nonlocal free
        nodes[0] += 1
        budget[0] -= 1
        if nodes[0] > _PACK_NODE_BUDGET or budget[0] <= 0:
            return False
        if k == len(order):
            return True
        if pending[k] > free + _EPS:
            return False
        idx = order[k]
        width = widths[idx]
        seen: set = set()
        for j in range(len(caps)):
            # Two sheets in the same state are the same choice; trying both is
            # what makes a naive search exponential on repeated sheets. The
            # floats are sums of the same values in the same order, so equality
            # is the right test and rounding would only cost time.
            state = (used[j], caps[j])
            if state in seen:
                continue
            seen.add(state)
            need = width + (kerf if used[j] > 0 else 0.0)
            if used[j] + need <= caps[j] + _EPS:
                used[j] += need
                free -= need
                bins[j].append(idx)
                if place(k + 1):
                    return True
                bins[j].pop()
                used[j] -= need
                free += need
        return False

    return bins if place(0) else None


# Orders tried when filling a strip. A strip is a wide, shallow rectangle and
# which pieces get in is a real decision: on pre-order 132 the sheet with the
# 151 mm strip takes three 600s (0.216 m²) or two 760s (0.182 m²), and every
# descending order in ``constructors.SORT_KEYS`` picks the 760s. Ascending
# orders exist here for exactly that reason. Ordering only — every order is
# packed by the same guillotine packer, and the most area wins.
_STRIP_ORDERS: Tuple[str, ...] = (
    "width_desc",
    "width_asc",
    "height_desc",
    "area_desc",
    "area_asc",
)

_STRIP_SORTS: Dict[str, Callable[[Piece], tuple]] = {
    "width_desc": lambda p: (-p.width, -p.height, p.id),
    "width_asc": lambda p: (p.width, -p.height, p.id),
    "height_desc": lambda p: (-p.height, -p.width, p.id),
    "area_desc": lambda p: (-p.area, -p.width, p.id),
    "area_asc": lambda p: (p.area, -p.width, p.id),
}


def _fill_strip(
    items: Sequence[_Item],
    spec: BinSpec,
    width: float,
    height: float,
    kerf: float,
    min_rect_size: float,
) -> Tuple[List[PlacedPiece], List[Cut], List[Rectangle], List[_Item]]:
    """Packs what it can of ``items`` into a ``width`` x ``height`` strip.

    The strip is handed to the ordinary guillotine packer as a pseudo bin with
    no trims — the trims were already taken off the sheet — so the pattern it
    produces is a real guillotine pattern and the leftovers are real. Returns
    what it placed, in strip-local coordinates, plus the items left over.
    """
    if not items or width <= 0 or height <= 0:
        return [], [], [], list(items)
    strip_spec = BinSpec(
        key=spec.key,
        width=width,
        height=height,
        thickness=spec.thickness,
        cost_per_unit=0.0,
    )
    # The strip decides orientation for itself: a piece sent here lies down by
    # construction, and letting the packer spin it back upright would undo the
    # very choice that freed the lane.
    pool = [
        Piece(
            id=item.piece.id,
            width=item.width,
            height=item.height,
            quantity=1,
            can_rotate=False,
            priority=item.piece.priority,
        )
        for item in items
    ]
    by_id = {item.piece.id: item for item in items}
    strip_params = CuttingParameters(kerf=kerf)
    best: Optional[Tuple[float, str, List[PlacedPiece], List[Cut], List[Rectangle]]] = (
        None
    )
    for name in _STRIP_ORDERS:
        try:
            packer = GuillotineOptimizer(
                material=strip_spec.to_material(),
                cutting_params=strip_params,
                split_rule=SplitRule.SHORTER_LEFTOVER_AXIS,
                selection=Selection.BEST_AREA_FIT,
                min_rect_size=min_rect_size,
            )
        except ValueError:
            return [], [], [], list(items)
        ordered = sorted(pool, key=_STRIP_SORTS[name])
        placed, _rest = packer.optimize(ordered, presorted=True)
        area = sum(p.width * p.height for p in placed)
        # Ties break on the order name so the winner never depends on float
        # comparison order.
        if best is None or (area, name) > (best[0], best[1]):
            best = (area, name, placed, packer.cuts, packer.remainders)
    assert best is not None
    _area, _name, placed, cuts, rects = best
    taken = {pp.piece.id for pp in placed}
    # The packer worked on stand-in pieces; hand back the real ones so ids and
    # original dimensions travel with the placement.
    restored = [
        PlacedPiece(
            piece=by_id[pp.piece.id].piece,
            x=pp.x,
            y=pp.y,
            width=pp.width,
            height=pp.height,
            rotated=by_id[pp.piece.id].rotated,
        )
        for pp in placed
    ]
    left = [item for item in items if item.piece.id not in taken]
    return restored, cuts, rects, left


def _emit(
    sheet: _Sheet,
    strip: Tuple[List[PlacedPiece], List[Cut], List[Rectangle]],
    params: CuttingParameters,
    min_rect_size: float,
) -> BinFill:
    """Materializes one sheet: placements, guillotine cuts and leftovers.

    The sheet is cut in two bands. The bottom band is ripped into lanes and each
    lane crosscut into its pieces; the top band, when it carries anything, is
    whatever the packer made of it. That single horizontal cut has to clear the
    TALLEST lane, which is why the height a shorter lane leaves unused stays with
    that lane as a leftover and is not reachable from the strip.
    """
    kerf = max(0.0, params.kerf)
    left = max(0.0, params.left_trim)
    bottom = max(0.0, params.bottom_trim)
    usable_w, usable_h = _usable(sheet.spec, params)

    placed: List[PlacedPiece] = []
    cuts: List[Cut] = []
    rects: List[Rectangle] = []

    def keep(rect: Rectangle) -> None:
        if min(rect.width, rect.height) >= min_rect_size:
            rects.append(rect)

    band = max((lane.height for lane in sheet.lanes), default=0.0)

    x = 0.0
    for lane in sheet.lanes:
        y = 0.0
        for item in lane.items:
            placed.append(
                PlacedPiece(
                    piece=item.piece,
                    x=left + x,
                    y=bottom + y,
                    width=item.width,
                    height=item.height,
                    rotated=item.rotated,
                )
            )
            y += item.height
            if y < lane.height - _EPS:
                # Crosscut separating this piece from the one above it.
                cuts.append(Cut(left + x, bottom + y, lane.width, is_horizontal=True))
                y += kerf
        if band - lane.height > kerf:
            keep(
                Rectangle(
                    left + x,
                    bottom + lane.height + kerf,
                    lane.width,
                    band - lane.height - kerf,
                )
            )
        x += lane.width
        if x < usable_w - _EPS:
            # First stage: the rip that separates this lane from the rest.
            cuts.append(Cut(left + x, bottom, band, is_horizontal=False))
            x += kerf

    tail = usable_w - x
    if tail > 0:
        keep(Rectangle(left + x, bottom, tail, band))

    strip_placed, strip_cuts, strip_rects = strip
    free = usable_h - band - kerf
    if strip_placed or strip_cuts or strip_rects:
        # The crosscut that opens the top band, taken across the whole sheet.
        cuts.append(Cut(left, bottom + band, usable_w, is_horizontal=True))
        moved_pieces, moved_cuts, moved_rects = _translate(
            strip_placed, strip_cuts, strip_rects, left, bottom + band + kerf
        )
        placed.extend(moved_pieces)
        cuts.extend(moved_cuts)
        for rect in moved_rects:
            keep(rect)
    elif free > 0:
        keep(Rectangle(left, bottom + band + kerf, usable_w, free))

    return BinFill(spec=sheet.spec, placed=placed, remainders=rects, cuts=cuts)


def _classes(
    bins: Sequence[BinSpec], params: CuttingParameters
) -> Optional[Tuple[List[Tuple[BinSpec, float]], float]]:
    """Bin classes that can carry full-height lanes, plus that height."""
    infinite = [b for b in bins if b.count is None]
    if not infinite:
        return None
    tallest = max(round(_usable(b, params)[1], 6) for b in infinite)
    usable = [
        (b, _usable(b, params)[0])
        for b in infinite
        if round(_usable(b, params)[1], 6) == tallest
    ]
    # One entry per distinct (width, cost): two identical specs would only
    # double the enumeration.
    seen: Dict[Tuple[float, float], Tuple[BinSpec, float]] = {}
    for spec, width in usable:
        seen.setdefault((round(width, 6), round(spec.cost_per_unit, 6)), (spec, width))
    classes = sorted(seen.values(), key=lambda c: (-c[1], c[0].cost_per_unit))
    if not classes or len(classes) > 2:
        return None
    if tallest <= 0 or any(width <= 0 for _spec, width in classes):
        return None
    return classes, tallest


def _standing(
    pieces: Sequence[Piece], widest: float, height: float
) -> Optional[List[_Item]]:
    """Every piece stood on its narrow side, or ``None`` if one cannot stand."""
    out: List[_Item] = []
    for piece in pieces:
        chosen = None
        for width, tall, rotated in _orientations(piece):
            if width <= widest + _EPS and tall <= height + _EPS:
                chosen = _Item(piece=piece, width=width, height=tall, rotated=rotated)
                break
        if chosen is None:
            return None
        out.append(chosen)
    return out


def _lying(item: _Item) -> _Item:
    """The same piece turned onto its wide side, for a strip.

    A piece that cannot rotate has only the one orientation, and for a low, wide
    piece (a rail, a toe-kick) that orientation is already the lying one — which
    is exactly why its group is a strip candidate in the first place.
    """
    options = _orientations(item.piece)
    if len(options) == 1:
        width, tall, rotated = options[0]
    else:
        width, tall, rotated = (
            options[1] if options[0][2] == item.rotated else options[0]
        )
    return _Item(piece=item.piece, width=width, height=tall, rotated=rotated)


def _width_groups(
    items: Sequence[_Item], height: float, kerf: float
) -> List[Tuple[float, List[_Item], float]]:
    """Items grouped by width, worst lane fill first.

    The fill of a width's lanes is what says whether it deserves lanes at all: a
    group of three 760x120 rails fills 13 % of the lanes it would cost, while a
    group of thirty 222x607 sides fills 84 %. The order is the candidate order
    for sending a whole group to the strips instead.
    """
    by_width: Dict[float, List[_Item]] = {}
    for item in items:
        by_width.setdefault(round(item.width, 6), []).append(item)
    groups = []
    for width in sorted(by_width):
        members = by_width[width]
        lanes = _stack(members, height, kerf)
        fill = (
            sum(lane.height for lane in lanes) / (len(lanes) * height) if lanes else 1.0
        )
        groups.append((width, members, fill))
    groups.sort(key=lambda g: (g[2], -g[0]))
    return groups


def lane_plan(
    pieces: Sequence[Piece],
    bins: Sequence[BinSpec],
    params: CuttingParameters,
    *,
    below_cost: float,
    max_sheets: int = 100,
    min_rect_size: float = 0.1,
) -> Optional[List[BinFill]]:
    """A complete plan billing strictly less than ``below_cost``, or ``None``.

    Every piece is placed or the candidate is discarded — a partial plan is not
    an answer to "is there a cheaper partition".
    """
    if not pieces or not bins:
        return None
    resolved = _classes(bins, params)
    if resolved is None:
        return None
    classes, height = resolved
    widest = max(width for _spec, width in classes)
    kerf = max(0.0, params.kerf)

    ordered = sorted(pieces, key=lambda p: p.id)
    standing = _standing(ordered, widest, height)
    if standing is None:
        return None

    groups = _width_groups(standing, height, kerf)

    budget = [_TOTAL_NODE_BUDGET]
    best: Optional[List[BinFill]] = None
    best_cost = below_cost
    # Two nested enumerations, both counted in candidates: how many width groups
    # are too wasteful to deserve lanes, and how many rotatables lie down.
    for s in range(min(len(groups) - 1, STRIP_GROUP_CANDIDATES) + 1):
        if budget[0] <= 0:
            break
        to_strip = {id(item) for _w, members, _f in groups[:s] for item in members}
        lane_items = [it for it in standing if id(it) not in to_strip]
        strip_items = [_lying(it) for it in standing if id(it) in to_strip]
        if not lane_items:
            continue

        # Rotatables still standing, widest-standing first: those are the ones
        # whose lane is most expensive to keep.
        swappable = [
            i
            for i, it in enumerate(lane_items)
            if it.piece.can_rotate and it.width < it.height
        ]
        swappable.sort(
            key=lambda i: (
                -lane_items[i].height,
                lane_items[i].width,
                lane_items[i].piece.id,
            )
        )
        for g in range(min(len(swappable), LAY_DOWN_CANDIDATES) + 1):
            if budget[0] <= 0:
                break
            lying = set(swappable[:g])
            plan = _plan_for(
                [it for i, it in enumerate(lane_items) if i not in lying],
                strip_items + [_lying(lane_items[i]) for i in sorted(lying)],
                classes,
                height,
                kerf,
                params,
                below_cost=best_cost,
                max_sheets=min(max_sheets, _MAX_SHEETS_TRIED),
                min_rect_size=min_rect_size,
                budget=budget,
            )
            if plan is not None:
                cost = sum(fill.spec.cost_per_unit for fill in plan)
                if cost < best_cost - _EPS and _is_cuttable(plan, params):
                    best, best_cost = plan, cost
    return best


def _is_cuttable(fills: Sequence[BinFill], params: CuttingParameters) -> bool:
    """Would the shop actually be able to cut this? Checked, not trusted.

    Everything else in this module reasons about widths and costs; this reasons
    about the only thing that reaches the saw. ``_emit`` computes coordinates by
    hand, and a cost comparison cannot tell a cheaper plan from a plan whose
    pieces overlap — the count check upstream cannot either, since a plan can
    hold every piece and still stack two of them on the same square millimetre.
    That failure does not cost a board, it costs a cut batch, so it is worth
    O(k²) per sheet on the rare plan that is about to be adopted.

    The checks are ``sheet_check``'s — the ones ``tests/unit/cutting_invariants.py``
    asserts and the ones a hand-edited sheet is held to — which is deliberate:
    the contract a candidate ships under should be the contract its tests read.
    """
    return all(
        is_cuttable(fill.spec.width, fill.spec.height, fill.placed, params)
        for fill in fills
    )


def _plan_for(
    lane_items: Sequence[_Item],
    strip_items: Sequence[_Item],
    classes: Sequence[Tuple[BinSpec, float]],
    height: float,
    kerf: float,
    params: CuttingParameters,
    *,
    below_cost: float,
    max_sheets: int,
    min_rect_size: float,
    budget: List[int],
) -> Optional[List[BinFill]]:
    """One (lane set, strip set) split, planned end to end."""
    if not lane_items:
        return None
    by_width: Dict[float, List[_Item]] = {}
    for item in lane_items:
        by_width.setdefault(round(item.width, 6), []).append(item)

    lanes: List[_Lane] = []
    for width in sorted(by_width, reverse=True):
        built = _stack(by_width[width], height, kerf)
        if not built:
            return None
        lanes.extend(built)
    # Widest lane first is the order ``_pack_widths`` searches in; keeping the
    # list sorted makes the emitted sheets read the same way.
    lanes.sort(key=lambda lane: (-lane.width, -lane.height, lane.items[0].piece.id))

    assignment = _pack_widths(
        [lane.width for lane in lanes], classes, kerf, below_cost, max_sheets, budget
    )
    if assignment is None:
        return None

    sheets = [
        _Sheet(spec=spec, lanes=[lanes[i] for i in sorted(idxs)])
        for spec, idxs in assignment
        if idxs
    ]
    if not sheets:
        return None

    # The strips are whatever each sheet's tallest lane leaves above it. Pieces
    # go to the first sheet that can hold them, sheets in plan order, so the
    # result never depends on dictionary or set iteration.
    remaining = list(strip_items)
    strips: List[Tuple[List[PlacedPiece], List[Cut], List[Rectangle]]] = []
    for sheet in sheets:
        usable_w, usable_h = _usable(sheet.spec, params)
        band = max(lane.height for lane in sheet.lanes)
        free = usable_h - band - kerf
        if free <= 0 or not remaining:
            strips.append(([], [], []))
            continue
        placed, cuts, rects, remaining = _fill_strip(
            remaining, sheet.spec, usable_w, free, kerf, min_rect_size
        )
        strips.append((placed, cuts, rects))
    if remaining:
        return None

    return [
        _emit(sheet, strips[i], params, min_rect_size) for i, sheet in enumerate(sheets)
    ]
