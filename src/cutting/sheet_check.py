"""Physical validity of a sheet laid out by hand, and where a piece may still go.

Everything the search emits is valid by construction, and is only checked in the
tests (``tests/unit/cutting_invariants.py``) or, for the one candidate that
computes coordinates by hand, right before it is adopted (``lanes._is_cuttable``).
A sheet the seller rearranged has no construction behind it, so it is checked
here: with those same assertions, plus the one the others never had to state,
that the saw can actually free every piece. That last check is not new code
either. ``consolidate.derive_tree`` is how the engine already derives every
board's cut tree from its final placements, so a hand-edited sheet gets its cuts
and its leftovers from the very same function, and "there is no tree" is exactly
"it cannot be cut".

Two consequences shape this module:

- **The cut tree is always derived, never edited.** With the pieces fixed, a
  different tree only reshapes the leftovers (and the saw travel), never what
  fits: moving a piece into free space "breaks" whatever cut was in the way, on
  its own. The one wish placements cannot express — "keep this offcut whole" —
  is a *whole offcut*: a rectangle of free space the tree has to cut out in one
  piece, fed to ``consolidate`` as a virtual piece and handed back as a
  leftover. It is a wish about FREE space, never an obstacle: a piece may still
  be placed on it, and then the offcut is no longer kept whole
  (``Placement.uses_whole_offcuts``).
- **"Where can this piece go" is answered here**, not in the browser, so there is
  a single definition of valid. The browser snaps to what this returns.

Framework-free, like the rest of ``src/cutting/``.
"""

from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from src.cutting.consolidate import (
    DEFAULT_MIN_USABLE_OFFCUT,
    DEFAULT_NODE_BUDGET,
    derive_tree,
)
from src.cutting.models import (
    Cut,
    CuttingLayout,
    Material,
    Piece,
    PlacedPiece,
    Rectangle,
)
from src.cutting.parameters import CuttingParameters

_EPS = 1e-6

# (x0, y0, x1, y1)
Box = Tuple[float, float, float, float]

# Why a sheet cannot be cut. Stable strings: the API reports them as they are.
OUT_OF_BOUNDS = "out_of_bounds"
INSIDE_TRIM = "inside_trim"
ROTATION_NOT_ALLOWED = "rotation_not_allowed"
WRONG_DIMENSIONS = "wrong_dimensions"
OVERLAP = "overlap"
KERF = "kerf"
WHOLE_OFFCUT_OVERLAP = "whole_offcut_overlap"
NOT_GUILLOTINE = "not_guillotine"
# ``derive_tree`` ran out of nodes before deciding. Not a proof of anything, but
# a sheet nobody can verify is not one to send to the saw, so it is refused.
UNVERIFIABLE = "unverifiable"

# Full tree derivations one ``placement_candidates`` call may spend on the
# positions that straddle several leftovers (the rest are valid by
# construction). Counted in derivations, never in wall clock, so the answer is
# the same on every machine.
DEFAULT_MAX_FULL_CHECKS = 48

# The four directions a leftover can be extended in, in the sheet's own axes:
# ``x`` runs along the width and ``y`` along the height (the largo).
DIRECTIONS = ("x+", "x-", "y+", "y-")


@dataclass(frozen=True)
class Violation:
    """One reason a sheet cannot be cut, and the pieces it concerns."""

    code: str
    piece_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Placement:
    """A position a piece may take: its corner and its orientation.

    ``uses_whole_offcuts`` lists the whole offcuts (indices into the sheet's
    list) the piece would land on. Putting a piece there uses the offcut, so it
    is no longer kept whole.
    """

    x: float
    y: float
    rotated: bool
    uses_whole_offcuts: Tuple[int, ...] = ()


def usable_bounds(width: float, height: float, params: CuttingParameters) -> Box:
    """The part of a sheet pieces may occupy: the sheet minus its trims."""
    return (
        params.left_trim,
        params.bottom_trim,
        width - params.right_trim,
        height - params.top_trim,
    )


def _box(p) -> Box:
    return (p.x, p.y, p.x + p.width, p.y + p.height)


def _separation(a: Box, b: Box) -> float:
    """How far apart two boxes are on the axis that separates them best.

    Negative when they overlap. The pair needs a blade's width on at least one
    axis, which is exactly this number being ``>= kerf``.
    """
    gap_x = max(b[0] - a[2], a[0] - b[2])
    gap_y = max(b[1] - a[3], a[1] - b[3])
    return max(gap_x, gap_y)


def iter_violations(
    width: float,
    height: float,
    placed: Sequence[PlacedPiece],
    params: CuttingParameters,
    whole_offcuts: Sequence[Rectangle] = (),
) -> Iterator[Violation]:
    """Everything wrong with a sheet short of the guillotine question.

    Bounds and trims, rotation legality, pairwise overlap and kerf — the
    assertions ``tests/unit/cutting_invariants.py`` makes on every layout the
    engine emits — plus a whole offcut being cut into. A generator, so a caller
    that only needs a yes/no (``is_cuttable``) stops at the first one.
    """
    x0, y0, x1, y1 = usable_bounds(width, height, params)
    kerf = max(0.0, params.kerf)
    boxes = [_box(pp) for pp in placed]
    for pp, (bx0, by0, bx1, by1) in zip(placed, boxes):
        if bx0 < -_EPS or by0 < -_EPS or bx1 > width + _EPS or by1 > height + _EPS:
            yield Violation(OUT_OF_BOUNDS, (pp.piece.id,))
        elif bx0 < x0 - _EPS or by0 < y0 - _EPS or bx1 > x1 + _EPS or by1 > y1 + _EPS:
            yield Violation(INSIDE_TRIM, (pp.piece.id,))
        if pp.rotated:
            if not pp.piece.can_rotate:
                yield Violation(ROTATION_NOT_ALLOWED, (pp.piece.id,))
            if (pp.width, pp.height) != (pp.piece.height, pp.piece.width):
                yield Violation(WRONG_DIMENSIONS, (pp.piece.id,))
        elif (pp.width, pp.height) != (pp.piece.width, pp.piece.height):
            yield Violation(WRONG_DIMENSIONS, (pp.piece.id,))
    for i in range(len(placed)):
        a = boxes[i]
        for j in range(i + 1, len(placed)):
            separation = _separation(a, boxes[j])
            if separation < kerf - _EPS:
                ids = (placed[i].piece.id, placed[j].piece.id)
                yield Violation(OVERLAP if separation < -_EPS else KERF, ids)
    whole_boxes = [_box(r) for r in whole_offcuts]
    for i, r in enumerate(whole_boxes):
        if r[0] < x0 - _EPS or r[1] < y0 - _EPS or r[2] > x1 + _EPS or r[3] > y1 + _EPS:
            yield Violation(WHOLE_OFFCUT_OVERLAP)
        for pp, b in zip(placed, boxes):
            if _separation(r, b) < kerf - _EPS:
                yield Violation(WHOLE_OFFCUT_OVERLAP, (pp.piece.id,))
        for other in whole_boxes[i + 1 :]:
            if _separation(r, other) < kerf - _EPS:
                yield Violation(WHOLE_OFFCUT_OVERLAP)


def is_cuttable(
    width: float,
    height: float,
    placed: Sequence[PlacedPiece],
    params: CuttingParameters,
) -> bool:
    """Whether nothing short of the guillotine question is wrong with a sheet."""
    return next(iter_violations(width, height, placed, params), None) is None


def realize_sheet(
    material: Material,
    placed: Sequence[PlacedPiece],
    params: CuttingParameters,
    whole_offcuts: Sequence[Rectangle] = (),
    *,
    min_rect_size: float = 0.1,
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> Tuple[Optional[CuttingLayout], List[Violation]]:
    """The sheet as the saw would cut it, or every reason it cannot be.

    Returns ``(layout, [])`` with the derived ``cuts`` and ``remainders`` — the
    tree's own leftovers first, then the whole offcuts, untouched — or
    ``(None, violations)``. The tree is the one ``consolidate`` would pick for
    these placements, so a sheet nobody edited comes back as the engine made it.
    """
    violations = list(
        iter_violations(material.width, material.height, placed, params, whole_offcuts)
    )
    if violations:
        return None, violations
    bounds = usable_bounds(material.width, material.height, params)
    boxes = [_box(pp) for pp in placed] + [_box(r) for r in whole_offcuts]
    cuts: List[Cut] = []
    leftovers: List[Rectangle] = []
    if boxes:
        plan, exhausted = derive_tree(
            boxes,
            bounds,
            params,
            min_rect_size=min_rect_size,
            min_usable_offcut=min_usable_offcut,
            node_budget=node_budget,
        )
        if plan is None:
            return None, [Violation(UNVERIFIABLE if exhausted else NOT_GUILLOTINE)]
        cuts, leftovers, _ = plan
    else:
        x0, y0, x1, y1 = bounds
        leftovers = [Rectangle(x0, y0, x1 - x0, y1 - y0)]
    layout = CuttingLayout(
        material=material,
        placed_pieces=list(placed),
        remainders=list(leftovers)
        + [Rectangle(r.x, r.y, r.width, r.height) for r in whole_offcuts],
        cuts=list(cuts),
    )
    return layout, []


def free_rectangles(bounds: Box, obstacles: Sequence[Box], kerf: float) -> List[Box]:
    """The maximal empty rectangles of a sheet (MaxRects).

    Every obstacle is inflated by the kerf on all four sides before it is cut
    out, which encodes the pairwise rule exactly: a box clear of the inflated
    obstacle is a blade's width away from it on at least one axis. The sheet's
    usable edge takes no kerf — a piece may sit flush against the trim.
    """
    free: List[Box] = []
    if bounds[2] - bounds[0] > _EPS and bounds[3] - bounds[1] > _EPS:
        free.append(bounds)
    for ox0, oy0, ox1, oy1 in obstacles:
        ix0, iy0, ix1, iy1 = ox0 - kerf, oy0 - kerf, ox1 + kerf, oy1 + kerf
        split: List[Box] = []
        kept: List[Box] = []
        for f in free:
            fx0, fy0, fx1, fy1 = f
            if (
                ix0 >= fx1 - _EPS
                or ix1 <= fx0 + _EPS
                or iy0 >= fy1 - _EPS
                or iy1 <= fy0 + _EPS
            ):
                kept.append(f)
                continue
            if ix0 > fx0 + _EPS:
                split.append((fx0, fy0, ix0, fy1))
            if ix1 < fx1 - _EPS:
                split.append((ix1, fy0, fx1, fy1))
            if iy0 > fy0 + _EPS:
                split.append((fx0, fy0, fx1, iy0))
            if iy1 < fy1 - _EPS:
                split.append((fx0, iy1, fx1, fy1))
        # Only the new rectangles need the containment test: an untouched one was
        # already maximal, so it cannot sit inside a piece of a rectangle that
        # used to be its peer.
        pieces = sorted(set(split))
        free = kept + [
            s
            for s in pieces
            if not any(_swallows(o, s) for o in kept)
            and not any(_swallows(o, s) for o in pieces)
        ]
    return sorted(free, key=lambda b: (b[1], b[0], b[3], b[2]))


def _swallows(outer: Box, inner: Box) -> bool:
    """``outer`` makes ``inner`` redundant. Of two equal-within-epsilon boxes
    only the first in sort order survives, so neither erases the other."""
    if outer == inner or not _contains(outer, inner):
        return False
    return not _contains(inner, outer) or outer < inner


def _contains(outer: Box, inner: Box) -> bool:
    return (
        outer[0] <= inner[0] + _EPS
        and outer[1] <= inner[1] + _EPS
        and outer[2] >= inner[2] - _EPS
        and outer[3] >= inner[3] - _EPS
    )


def _freeable_in_leftover(
    box: Box, leftovers: Sequence[Rectangle], kerf: float
) -> bool:
    """Whether ``box`` sits inside one leftover where the saw can free it.

    A leftover is a leaf of the derived tree, so the cuts that carved it out cross
    nothing inside it, and a piece placed there only needs the region's own cuts.
    On the far side any slack works (``consolidate`` lets a cut overhang a strip
    narrower than the blade), but on the near side the cut has to land strictly
    inside the region: the slack is either none at all or more than a kerf. Held
    to the same rule ``consolidate._candidates`` applies, so this shortcut can
    never call valid what the full derivation would refuse.
    """
    for r in leftovers:
        if not _contains((r.x, r.y, r.x + r.width, r.y + r.height), box):
            continue
        near_x = box[0] - r.x
        near_y = box[1] - r.y
        if (near_x <= _EPS or near_x > kerf + _EPS) and (
            near_y <= _EPS or near_y > kerf + _EPS
        ):
            return True
    return False


def placement_candidates(
    material: Material,
    others: Sequence[PlacedPiece],
    piece: Piece,
    params: CuttingParameters,
    whole_offcuts: Sequence[Rectangle] = (),
    *,
    first_only: bool = False,
    current: Optional[Placement] = None,
    max_full_checks: int = DEFAULT_MAX_FULL_CHECKS,
    min_rect_size: float = 0.1,
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT,
) -> Tuple[List[Placement], List[Box]]:
    """Where ``piece`` can go on a sheet already holding ``others``.

    Returns ``(placements, free_rectangles)``. The candidates are the corners of
    the maximal free rectangles the piece fits in, in each orientation it is
    allowed: flush against the trim or a blade's width from a neighbour, which
    are the positions that keep the cuts straight and the leftovers usable. A
    corner inside one leftover of the current tree is valid by construction; one
    that straddles several — the "this cut is in the way" case — is valid only if
    the whole sheet still derives a tree, which is checked here, up to
    ``max_full_checks`` of them. ``first_only`` stops at the first valid position
    per orientation, for the "which sheets can take it" question.

    A whole offcut is free space here, not an obstacle: the seller who grew an
    offcut to make room expects to use that room. A position that lands on one
    is checked without it, and says so in ``uses_whole_offcuts``.

    ``current`` is where the piece sits now, when it is on this sheet: it is
    always offered (first), because putting a piece back where it was must
    always work, and so is the piece turned on that same corner whenever that is
    valid, which is what "rotate it here" means. The corners alone miss the
    piece's own spot for about one piece in seven — a piece flush against a
    neighbour on one side only sits on no corner of the free space around it.

    ``others`` must not include ``piece`` itself.
    """
    kerf = max(0.0, params.kerf)
    bounds = usable_bounds(material.width, material.height, params)
    obstacles = [_box(pp) for pp in others]
    free = free_rectangles(bounds, obstacles, kerf)
    whole_list = list(whole_offcuts)

    base, _ = realize_sheet(
        material,
        others,
        params,
        whole_offcuts,
        min_rect_size=min_rect_size,
        min_usable_offcut=min_usable_offcut,
    )
    whole_boxes = {_box(r) for r in whole_offcuts}
    leftovers = (
        [r for r in base.remainders if _box(r) not in whole_boxes] if base else []
    )

    orientations = [(False, piece.width, piece.height)]
    if piece.can_rotate and piece.width != piece.height:
        orientations.append((True, piece.height, piece.width))

    placements: List[Placement] = []
    full_checks = 0

    def checked(x: float, y: float, rotated: bool) -> Optional[Placement]:
        w, h = (piece.height, piece.width) if rotated else (piece.width, piece.height)
        box = (x, y, x + w, y + h)
        used = tuple(
            i
            for i, r in enumerate(whole_list)
            if _separation(box, _box(r)) < kerf - _EPS
        )
        layout, _ = realize_sheet(
            material,
            [*others, PlacedPiece(piece, x, y, w, h, rotated)],
            params,
            [r for i, r in enumerate(whole_list) if i not in used],
            min_rect_size=min_rect_size,
            min_usable_offcut=min_usable_offcut,
        )
        return Placement(x, y, rotated, uses_whole_offcuts=used) if layout else None

    if current is not None:
        # Where it is, and turned on that same corner: "put it back" and "turn it
        # here" must not depend on either landing on a corner of the free space.
        spots = [(current.x, current.y, current.rotated)]
        if piece.can_rotate and piece.width != piece.height:
            spots.append((current.x, current.y, not current.rotated))
        for x, y, rotated in spots:
            spot = checked(x, y, rotated)
            if spot is not None:
                placements.append(spot)
    for rotated, w, h in orientations:
        if first_only and any(p.rotated == rotated for p in placements):
            continue
        corners = set()
        for fx0, fy0, fx1, fy1 in free:
            if fx1 - fx0 < w - _EPS or fy1 - fy0 < h - _EPS:
                continue
            for x in (fx0, fx1 - w):
                for y in (fy0, fy1 - h):
                    corners.add((y, x))
        for y, x in sorted(corners):
            if any(
                p.rotated == rotated and abs(p.x - x) < _EPS and abs(p.y - y) < _EPS
                for p in placements
            ):
                continue
            box = (x, y, x + w, y + h)
            used = tuple(
                i
                for i, r in enumerate(whole_list)
                if _separation(box, _box(r)) < kerf - _EPS
            )
            # Only a position that keeps every whole offcut can use the current
            # tree's leftovers as its proof; one that uses some has to derive the
            # sheet without them.
            valid = not used and _freeable_in_leftover(box, leftovers, kerf)
            if not valid and base is not None and full_checks < max_full_checks:
                full_checks += 1
                trial = PlacedPiece(
                    piece=piece, x=x, y=y, width=w, height=h, rotated=rotated
                )
                layout, _ = realize_sheet(
                    material,
                    [*others, trial],
                    params,
                    [r for i, r in enumerate(whole_list) if i not in used],
                    min_rect_size=min_rect_size,
                    min_usable_offcut=min_usable_offcut,
                )
                valid = layout is not None
            if valid:
                placements.append(
                    Placement(x=x, y=y, rotated=rotated, uses_whole_offcuts=used)
                )
                if first_only:
                    break
    return placements, free


def leftover_extensions(
    material: Material,
    placed: Sequence[PlacedPiece],
    params: CuttingParameters,
    whole_offcuts: Sequence[Rectangle],
    leftover: Rectangle,
    *,
    min_rect_size: float = 0.1,
    min_usable_offcut: float = DEFAULT_MIN_USABLE_OFFCUT,
) -> List[Tuple[str, Rectangle]]:
    """How far a leftover can grow in each direction and still be cut whole.

    For each direction the leftover is pushed until the first piece (a blade's
    width short of it) or whole offcut in its way, or the trim; the grown
    rectangle is offered only when keeping it whole still derives a tree. This is
    "break that cut": growing the small offcut beside a piece to full height is
    the same sheet with the other cut made first.
    """
    kerf = max(0.0, params.kerf)
    x0, y0, x1, y1 = usable_bounds(material.width, material.height, params)
    own = _box(leftover)
    others = [r for r in whole_offcuts if _box(r) != own]
    obstacles = [
        (b[0] - kerf, b[1] - kerf, b[2] + kerf, b[3] + kerf)
        for b in [_box(pp) for pp in placed] + [_box(r) for r in others]
    ]
    lx0, ly0, lx1, ly1 = own

    def spans_x(o: Box) -> bool:
        return o[0] < lx1 - _EPS and o[2] > lx0 + _EPS

    def spans_y(o: Box) -> bool:
        return o[1] < ly1 - _EPS and o[3] > ly0 + _EPS

    grown = {
        "x+": (
            lx0,
            ly0,
            min([x1] + [o[0] for o in obstacles if spans_y(o) and o[0] >= lx1 - _EPS]),
            ly1,
        ),
        "x-": (
            max([x0] + [o[2] for o in obstacles if spans_y(o) and o[2] <= lx0 + _EPS]),
            ly0,
            lx1,
            ly1,
        ),
        "y+": (
            lx0,
            ly0,
            lx1,
            min([y1] + [o[1] for o in obstacles if spans_x(o) and o[1] >= ly1 - _EPS]),
        ),
        "y-": (
            lx0,
            max([y0] + [o[3] for o in obstacles if spans_x(o) and o[3] <= ly0 + _EPS]),
            lx1,
            ly1,
        ),
    }
    extensions: List[Tuple[str, Rectangle]] = []
    for direction in DIRECTIONS:
        gx0, gy0, gx1, gy1 = grown[direction]
        if (gx1 - gx0) * (gy1 - gy0) <= (lx1 - lx0) * (ly1 - ly0) + _EPS:
            continue
        rect = Rectangle(gx0, gy0, gx1 - gx0, gy1 - gy0)
        layout, _ = realize_sheet(
            material,
            placed,
            params,
            [*others, rect],
            min_rect_size=min_rect_size,
            min_usable_offcut=min_usable_offcut,
        )
        if layout is not None:
            extensions.append((direction, rect))
    return extensions
