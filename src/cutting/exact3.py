"""CP-SAT 3-stage guillotine DECISION model over a whole set of bins.

Why a second solver module
--------------------------
``exact.py`` answers "does this pool close in ONE bin?" with a **2-stage**
column model: the bin is ripped into columns and each column is crosscut into
stacked rows, one piece per row plus a trim. That family is a strict subset of
guillotine patterns, and its own docstring says the rest out loud — *a 3-stage
pattern can pack things this model cannot express*.

**The BARROCO DORADO cut list is what that costs**, and it is the case every
number below is measured on: 29 grain-locked pieces, 7.679 m2, on a 2070x2800
board. It is embedded as ``BARROCO_DORADO_POOL`` in
``tests/unit/test_cutting_search.py``, which is its only source of truth —
deliberately, so this stays readable after the quote it came from is gone. (It
came from a real shop file the commercial program bills as 1 board + 1 half.)

A 1 + 1/2 plan for it provably exists without rotating anything, and it needs the
full board packed to 93.4% with strips carrying 3-4 pieces of DIFFERENT heights
side by side — a 3-stage pattern. Asked for those 29 pieces in 1 full + 1 half,
``exact.solve_bin`` answers INFEASIBLE in all four orientations, and the greedy
portfolio places 16 of the full board's 18 pieces. The partition exists and
nothing else in the engine can realize it.

Two things this model does differently, and both are the point:

- **Three stages.** A column is crosscut into strips (second stage) and each
  strip carries several pieces side by side (third stage), each trimmed down to
  its own length when it is shorter than the strip.
- **Several bins at once.** The question that wins the half board is not "does
  the tail close in one more bin?" but "does the WHOLE pool fit in 1 full + 1
  half?" — a partition question no per-bin call can ask, because the subset that
  has to land on the full board is exactly what is being decided.

What it is not
--------------
It is a **decision** model, never an optimization one, and that is a measured
choice rather than a simplification. Maximizing placed area with three stages
does not converge: on that cut list it needs 60s at ``num_workers=1`` to reach the
subset that works, and at 15s it returns a DENSER first board whose remainder no
longer fits the half — more area is not the objective. Deciding the same pool
costs **2.8s**, and a bin set with no answer is usually refused in hundredths.

Like ``exact.py``, a result is only ever an ADDITIONAL candidate and ``None``
never means "proven impossible" — the tree is capped (see ``TREE_SIZES``), so a
pattern needing a wider tree is simply out of reach here.

Determinism is the same hard requirement: ``num_workers=1``, a fixed seed and
``max_deterministic_time`` (a work unit, never wall clock), plus a canonical
reconstruction. The reconstruction matters MORE here than in ``exact.py``,
because a 3-stage answer carries more arbitrary detail: the strip index, the
order of pieces inside a strip and the strip height (the model only bounds it
from below). All three are re-derived from the content in ``_build_fills``. Not
doing that is how the engine once inherited an OR-Tools build's arbitrary pick
and billed 5 boards on macOS and 6 on linux for the same cut list.

**Where that stops, and why it is acceptable here.** Canonical reconstruction
makes one answer lay out canonically; it cannot make two DIFFERENT feasible
answers agree. A decision model has no objective, so every feasible assignment
is equally good to it and which pieces land on which sheet really does vary
between OR-Tools builds — measured, by perturbing the tie-break
(``tests/unit/test_cutting_exact3.py``). That is one notch wider than the
residual the engine already accepts for total cut length, and it is bounded in
the way that matters: the bin multiset is an INPUT here, never something the
solver chooses, so the bill is identical on every build and only the diagram
moves. Closing it would mean adding a tie-break objective — turning the cheap
decision back into the optimization this module exists to avoid (0.3s vs 60s).
Note the cache hash records ``exact_available()`` but not the binary, so a Redis
entry populated by one build and read by another can still serve a diagram this
process would not reproduce. Same caveat ``exact.py`` carries, same scope.
"""

import itertools
from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting.constructors import BinFill, PieceType, piece_type
from src.cutting.exact import _orientations, _scale_for, cp_model, is_available
from src.cutting.models import BinSpec, Cut, Piece, PlacedPiece, Rectangle
from src.cutting.parameters import CuttingParameters

# The cut tree the model may build: ``(columns, strips per column)``. A SMALL
# tree is a restriction, so anything found inside it is valid, and it is found
# far sooner. Measured on ``BARROCO_DORADO_POOL`` at ``num_workers=1``: **4x4 decides in
# 2.8s and 6x6 does not decide at all**, at any budget tried up to 8 work units.
# That is not a paradox — extra interchangeable columns buy expressiveness this
# question does not need and cost search on every pool — and it is why this is
# one size rather than a ladder: with the budget the battery supports (below) a
# second size is never reached anyway. Widening it means paying for it there.
TREE_SIZES: Tuple[Tuple[int, int], ...] = ((4, 4),)

# Above this the model stops being a cheap question. The pool that reaches here
# is a tail or a two-sheet neighbourhood, never a whole 250-piece quote.
MAX_PIECES = 60

# Work budgets, in CP-SAT deterministic units (never wall clock, so the answer
# is reproducible). ``PER_SOLVE`` caps one (tree size, orientation) attempt;
# ``TOTAL`` caps the whole ladder, which is what makes the NO answer affordable
# — without it the worst case is sizes x orientations x per-solve. On the
# reference cut list the winning attempt is the FIRST one tried and costs ~3
# units, so the total is set just wide enough to let a second attempt run.
# **The cost of this whole module is the attempts that FAIL.** Instrumented over
# the 80-job battery: 31 calls, and the 2 that win cost 0.2s TOTAL while the 29
# that lose cost 153s. Nothing about the pool predicts which is which (the
# density and piece-count distributions of the two groups overlap almost
# completely), so the only lever is how long a losing attempt is allowed to run.
#
# Measured back-to-back at ``--workers 1`` against a 160.3s baseline — the same
# 2 jobs won and 0 lost at every setting, so this buys latency only:
#
#     3/3 -> 243.0s (+51%)    4/4 -> 247.6s (+54%)
#     4/6 -> 300.2s (+87%)    5/8 -> 348.2s (+117%)
#
# 4/4 is where the curve flattens, with margin over the case that motivated the
# module (the reference cut list decides at 3 units). Equal values mean ONE attempt gets
# the budget: a second orientation would cost 33 points of CPU for nothing the
# battery can see, which puts real weight on ``_frame_order`` picking right the
# first time. Counted in work units, never wall clock, so it stays reproducible.
PER_SOLVE_DETERMINISTIC_TIME = 4.0
TOTAL_DETERMINISTIC_TIME = 4.0


class _Frame:
    """Integer view of one bin in its local (across, along) frame.

    ``across`` is the axis the first-stage rip advances along — it sizes the
    columns; ``along`` is the axis strips stack on. ``transposed`` swaps which
    sheet axis is which, so the same model expresses rip-first and crosscut-first.
    """

    def __init__(self, spec: BinSpec, params: CuttingParameters, scale: int):
        self.spec = spec
        self.scale = scale
        self.left = max(0.0, params.left_trim)
        self.bottom = max(0.0, params.bottom_trim)
        usable_w = spec.width - self.left - max(0.0, params.right_trim)
        usable_h = spec.height - self.bottom - max(0.0, params.top_trim)
        # Pieces and kerf round UP, the bin rounds DOWN: an integer-feasible
        # answer is always feasible in real millimetres, never the other way.
        self.W = int(usable_w * scale)
        self.H = int(usable_h * scale)
        self.K = -int(-max(0.0, params.kerf) * scale)  # ceil
        self.transposed = False

    @property
    def valid(self) -> bool:
        return self.W > 0 and self.H > 0

    def orient(self, transposed: bool) -> "_Frame":
        clone = object.__new__(_Frame)
        clone.__dict__.update(self.__dict__)
        clone.transposed = transposed
        return clone

    @property
    def across_max(self) -> int:
        return self.H if self.transposed else self.W

    @property
    def along_max(self) -> int:
        return self.W if self.transposed else self.H

    def up(self, value: float) -> int:
        """Scale a piece dimension UP (``_orientations`` calls this)."""
        return -int(-value * self.scale)

    def real(self, units: int) -> float:
        return units / self.scale

    def point(self, along_pos: int, across_pos: int) -> Tuple[float, float]:
        """Bin-local (along, across) -> sheet (x, y)."""
        if self.transposed:
            return self.left + self.real(along_pos), self.bottom + self.real(across_pos)
        return self.left + self.real(across_pos), self.bottom + self.real(along_pos)

    def rect(
        self, along_pos: int, across_pos: int, along: int, across: int
    ) -> Rectangle:
        x, y = self.point(along_pos, across_pos)
        if self.transposed:
            return Rectangle(x, y, self.real(along), self.real(across))
        return Rectangle(x, y, self.real(across), self.real(along))


def _grouped(pool: Sequence[Piece]) -> List[Tuple[PieceType, List[Piece]]]:
    """Pool grouped by piece type in a stable order (the model's identity)."""
    groups: Dict[PieceType, List[Piece]] = {}
    for piece in sorted(pool, key=lambda p: p.id):
        groups.setdefault(piece_type(piece), []).append(piece)
    return sorted(groups.items())


def _frame_order(
    frames: Sequence[_Frame], groups: Sequence[Tuple[PieceType, List[Piece]]]
) -> List[Tuple[bool, ...]]:
    """Transposition combinations to try, cheapest-looking first.

    Pure ordering: every combination is still reachable, so this cannot change
    WHICH answers exist, only how soon a feasible one is found. The rule is that
    a bin narrow relative to its widest piece almost certainly runs its first
    stage across the sheet — a half board whose widest piece nearly spans it has
    no room for two columns. On the reference cut list this puts the winning
    orientation first, which is the difference between 3.8s and 26s.
    """
    widest = max((p.width for _t, ps in groups for p in ps), default=0)
    prefer = [f.spec.width < 1.3 * widest for f in frames]
    combos = list(itertools.product((False, True), repeat=len(frames)))
    return sorted(combos, key=lambda c: (sum(x != p for x, p in zip(c, prefer)), c))


def _solve_once(
    groups, frames: Sequence[_Frame], n_cols: int, n_rows: int, deterministic_time, seed
):
    """One CP-SAT decision solve; returns ``(solver, vars)`` or ``None``."""
    model = cp_model.CpModel()
    # options[b][t] = feasible (across, along, rotated) of type t in bin b
    options = [
        [
            _orientations(ps[0], f, f.transposed, f.across_max, f.along_max)
            for _t, ps in groups
        ]
        for f in frames
    ]
    # counts[b][k][r][t][o]
    counts: List = []
    widths: List = []
    open_cols: List = []
    heights: List = []
    open_rows: List = []
    for b, f in enumerate(frames):
        alongs = sorted({al for opts in options[b] for _a, al, _r in opts})
        if not alongs:
            return None
        domain = cp_model.Domain.FromValues([0] + alongs)
        w_b = [model.NewIntVar(0, f.across_max, f"w{b}_{k}") for k in range(n_cols)]
        u_b = [model.NewBoolVar(f"u{b}_{k}") for k in range(n_cols)]
        h_b, o_b, c_b, area_terms = [], [], [], []
        for k in range(n_cols):
            h_k = [
                model.NewIntVarFromDomain(domain, f"h{b}_{k}_{r}")
                for r in range(n_rows)
            ]
            o_k = [model.NewBoolVar(f"o{b}_{k}_{r}") for r in range(n_rows)]
            c_k = []
            for r in range(n_rows):
                across_terms, present_terms, c_r = [], [], []
                for t_idx, (_t, pieces) in enumerate(groups):
                    per_type = []
                    for o_idx, (across, along, _rot) in enumerate(options[b][t_idx]):
                        qty = len(pieces)
                        count = model.NewIntVar(0, qty, f"q{b}_{k}_{r}_{t_idx}_{o_idx}")
                        present = model.NewBoolVar(f"p{b}_{k}_{r}_{t_idx}_{o_idx}")
                        model.Add(count <= qty * present)
                        model.Add(count >= 1).OnlyEnforceIf(present)
                        model.Add(present <= o_k[r])
                        # The strip is at least as long as anything inside it.
                        model.Add(h_k[r] >= along).OnlyEnforceIf(present)
                        across_terms.append((count, across))
                        present_terms.append(count)
                        area_terms.append((count, across * along))
                        per_type.append(count)
                    c_r.append(per_type)
                c_k.append(c_r)
                # Third stage: the strip's pieces sit side by side across it.
                model.Add(sum(c * (a + f.K) for c, a in across_terms) <= w_b[k] + f.K)
                model.Add(sum(present_terms) >= 1).OnlyEnforceIf(o_k[r])
                model.Add(sum(present_terms) == 0).OnlyEnforceIf(o_k[r].Not())
                model.Add(h_k[r] == 0).OnlyEnforceIf(o_k[r].Not())
                model.Add(o_k[r] <= u_b[k])
                if r:
                    # Strips are interchangeable: keep the used ones first.
                    model.AddImplication(o_k[r], o_k[r - 1])
            # Second stage: strips stack along the column.
            model.Add(
                sum(h_k[r] + f.K * o_k[r] for r in range(n_rows)) <= f.along_max + f.K
            )
            model.Add(w_b[k] == 0).OnlyEnforceIf(u_b[k].Not())
            if k:
                # Columns are interchangeable: force a canonical decreasing order.
                model.Add(w_b[k - 1] >= w_b[k])
                model.AddImplication(u_b[k], u_b[k - 1])
            h_b.append(h_k)
            o_b.append(o_k)
            c_b.append(c_k)
        # First stage: ripping the bin into columns.
        model.Add(
            sum(w_b[k] + f.K * u_b[k] for k in range(n_cols)) <= f.across_max + f.K
        )
        # Redundant but strong: what lands in a bin cannot exceed its usable
        # area. Measured on the reference cut list it is worth ~10x on its own.
        if area_terms:
            model.Add(sum(c * a for c, a in area_terms) <= f.across_max * f.along_max)
        widths.append(w_b)
        open_cols.append(u_b)
        heights.append(h_b)
        open_rows.append(o_b)
        counts.append(c_b)

    # Every instance placed exactly once, somewhere.
    for t_idx, (_t, pieces) in enumerate(groups):
        model.Add(
            sum(
                counts[b][k][r][t_idx][o]
                for b in range(len(frames))
                for k in range(n_cols)
                for r in range(n_rows)
                for o in range(len(options[b][t_idx]))
            )
            == len(pieces)
        )

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = deterministic_time
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return solver, options, counts, widths, open_cols, heights, open_rows


def _build_fills(
    solver,
    options,
    counts,
    widths,
    open_cols,
    heights,
    open_rows,
    groups,
    frames,
    n_cols,
    n_rows,
    min_rect_size,
) -> Optional[List[BinFill]]:
    """Turns a solved assignment into real placements, cuts and leftovers.

    **Canonical by construction.** Everything the solver reports that is
    arbitrary rather than meaningful is re-derived from the content before a
    single coordinate is computed: the strip height (the model only bounds it
    from below), the order of pieces inside a strip, the order of strips in a
    column and the column order and width. Concrete instances are attached only
    afterwards, so a piece's id follows the layout and never the solver's
    indices. Two OR-Tools builds returning different-but-equally-valid answers
    therefore lay out identically — the property that cost the engine a board
    per build before ``exact._build_fill`` learned the same trick.
    """
    fills: List[BinFill] = []
    cursors = [0] * len(groups)
    for b, frame in enumerate(frames):
        columns = []
        for k in range(n_cols):
            if not solver.Value(open_cols[b][k]):
                continue
            strips = []
            for r in range(n_rows):
                if not solver.Value(open_rows[b][k][r]):
                    continue
                items = []
                for t_idx in range(len(groups)):
                    for o_idx, (across, along, rot) in enumerate(options[b][t_idx]):
                        items.extend(
                            [(across, along, rot, t_idx)]
                            * solver.Value(counts[b][k][r][t_idx][o_idx])
                        )
                if not items:
                    continue
                # Widest first inside the strip, then by content: a canonical
                # order among interchangeable side-by-side positions.
                items.sort(key=lambda it: (-it[0], -it[1], it[3], it[2]))
                span = sum(it[0] for it in items) + frame.K * (len(items) - 1)
                strips.append((max(it[1] for it in items), span, tuple(items)))
            if not strips:
                continue
            strips.sort(key=lambda s: (-s[0], -s[1], s[2]))
            columns.append((max(s[1] for s in strips), tuple(strips)))
        columns.sort(key=lambda c: (-c[0], c[1]))

        placed: List[PlacedPiece] = []
        cuts: List[Cut] = []
        rects: List[Rectangle] = []

        def keep(candidate: Rectangle) -> None:
            if min(candidate.width, candidate.height) >= min_rect_size:
                rects.append(candidate)

        across_cursor = 0
        for width, strips in columns:
            along_cursor = 0
            for strip_along, _span, items in strips:
                item_cursor = across_cursor
                for across, along, rotated, t_idx in items:
                    piece = groups[t_idx][1][cursors[t_idx]]
                    cursors[t_idx] += 1
                    x, y = frame.point(along_cursor, item_cursor)
                    placed.append(
                        PlacedPiece(
                            piece=piece,
                            x=x,
                            y=y,
                            width=piece.height if rotated else piece.width,
                            height=piece.width if rotated else piece.height,
                            rotated=rotated,
                        )
                    )
                    # Fourth cut: trim the piece down when the strip is longer
                    # than it is. Same role the 2-stage model's third stage has.
                    overhang = strip_along - along
                    if overhang > 0:
                        tx, ty = frame.point(along_cursor + along, item_cursor)
                        cuts.append(
                            Cut(
                                tx,
                                ty,
                                frame.real(across),
                                is_horizontal=not frame.transposed,
                            )
                        )
                        if overhang > frame.K:
                            keep(
                                frame.rect(
                                    along_cursor + along + frame.K,
                                    item_cursor,
                                    overhang - frame.K,
                                    across,
                                )
                            )
                    item_cursor += across
                    if item_cursor < across_cursor + width:
                        # Third stage: separating this piece from its neighbour.
                        cx, cy = frame.point(along_cursor, item_cursor)
                        cuts.append(
                            Cut(
                                cx,
                                cy,
                                frame.real(strip_along),
                                is_horizontal=frame.transposed,
                            )
                        )
                        item_cursor += frame.K
                # What the strip did not use, to the far side of its last piece.
                tail = across_cursor + width - item_cursor
                if tail > 0:
                    keep(frame.rect(along_cursor, item_cursor, strip_along, tail))
                along_cursor += strip_along
                if along_cursor < frame.along_max:
                    # Second stage: the crosscut under the next strip.
                    cx, cy = frame.point(along_cursor, across_cursor)
                    cuts.append(
                        Cut(
                            cx,
                            cy,
                            frame.real(width),
                            is_horizontal=not frame.transposed,
                        )
                    )
                    along_cursor += frame.K
            if frame.along_max - along_cursor > 0:
                keep(
                    frame.rect(
                        along_cursor,
                        across_cursor,
                        frame.along_max - along_cursor,
                        width,
                    )
                )
            across_cursor += width
            if across_cursor < frame.across_max:
                # First stage: the rip separating this column from the rest.
                cx, cy = frame.point(0, across_cursor)
                cuts.append(
                    Cut(
                        cx,
                        cy,
                        frame.real(frame.along_max),
                        is_horizontal=frame.transposed,
                    )
                )
                across_cursor += frame.K
        if frame.across_max - across_cursor > 0:
            keep(
                frame.rect(
                    0, across_cursor, frame.along_max, frame.across_max - across_cursor
                )
            )
        fills.append(
            BinFill(spec=frame.spec, placed=placed, remainders=rects, cuts=cuts)
        )
    return fills


def fits_bins(
    pool: Sequence[Piece],
    specs: Sequence[BinSpec],
    params: CuttingParameters,
    *,
    deterministic_time: float = PER_SOLVE_DETERMINISTIC_TIME,
    total_deterministic_time: float = TOTAL_DETERMINISTIC_TIME,
    seed: int = 0,
    min_rect_size: float = 0.1,
    max_pieces: int = MAX_PIECES,
    tree_sizes: Sequence[Tuple[int, int]] = TREE_SIZES,
) -> Optional[List[BinFill]]:
    """Does the WHOLE ``pool`` fit in exactly this set of bins, in 3 stages?

    Returns one ``BinFill`` per spec, in the order given (a bin the answer
    leaves empty comes back with no pieces), or ``None`` when no answer was
    found inside the budget. ``None`` is NOT a proof of impossibility: the tree
    is capped and the solver may simply have run out of work units.

    Effort is bounded twice and counted only in work units and tree sizes, never
    wall clock, so the answer is reproducible for a given OR-Tools build.
    """
    if cp_model is None or not pool or not specs:
        return None
    if len(pool) > max_pieces:
        return None

    dims: List[float] = [max(0.0, params.kerf)]
    for spec in specs:
        dims.extend((spec.width, spec.height))
    for piece in pool:
        dims.extend((piece.width, piece.height))
    scale = _scale_for(dims)

    base = [_Frame(spec, params, scale) for spec in specs]
    if any(not f.valid for f in base):
        return None
    groups = _grouped(pool)

    order = _frame_order(base, groups)
    spent = 0.0
    for n_cols, n_rows in tree_sizes:
        for combo in order:
            allowance = min(deterministic_time, total_deterministic_time - spent)
            if allowance <= 0:
                return None
            frames = [f.orient(t) for f, t in zip(base, combo)]
            solved = _solve_once(groups, frames, n_cols, n_rows, allowance, seed)
            if solved is None:
                # A refused attempt still spent work; charging the allowance is
                # the conservative read (CP-SAT reports 0 for some exits) and
                # keeps the ladder from running away on a pool with no answer.
                spent += allowance
                continue
            solver, options, counts, widths, open_cols, heights, open_rows = solved
            fills = _build_fills(
                solver,
                options,
                counts,
                widths,
                open_cols,
                heights,
                open_rows,
                groups,
                frames,
                n_cols,
                n_rows,
                min_rect_size,
            )
            if fills and sum(len(f.placed) for f in fills) == len(pool):
                return fills
    return None


def fits_one_bin(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    *,
    deterministic_time: float = 6.0,
    seed: int = 0,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """3-stage twin of ``exact.fits_one_bin``, for a single bin."""
    fills = fits_bins(
        pool,
        [spec],
        params,
        deterministic_time=deterministic_time,
        seed=seed,
        min_rect_size=min_rect_size,
    )
    return fills[0] if fills else None


__all__ = ["fits_bins", "fits_one_bin", "is_available", "MAX_PIECES", "TREE_SIZES"]
