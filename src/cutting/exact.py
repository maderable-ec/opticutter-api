"""CP-SAT exact endgame: 2-stage guillotine subset selection for ONE bin.

The heuristic layer (``constructors.py`` + ``search.py``) closes most jobs, but
the audit measured its failure mode precisely: on packs above ~94% greedy repair
can't place the last handful of pieces, so the search settles for "N boards plus
one nearly empty extra". Deciding *exactly* which subset of the remaining pieces
fits one more board is a small combinatorial problem — small enough for a
solver, too hard for a greedy.

**The model is 2-stage guillotine**: the bin is ripped into parallel columns
(first stage) and each column is crosscut into stacked rows (second stage), with
an optional third-stage trim when a row is narrower than its column. Two-stage
patterns are a strict subset of guillotine patterns, so **feasibility is
constructive**: whatever the solver returns can be laid out with real saw cuts
(``_build_fill`` does exactly that). It is also *not* complete — a 3-stage
pattern can pack things this model cannot express — so an exact fill is only
ever an ADDITIONAL candidate, never a replacement for a heuristic fill that
already works, and ``None`` never means "proven impossible".

The model is written over piece **types**, not instances: identical pieces are
interchangeable, so one integer count per (type, column) removes the factorial
symmetry that would otherwise dominate the search (79 pieces of 13 types become
13 integer variables per column). Instances are re-attached deterministically
during reconstruction.

**Determinism is a hard requirement** (payloads are cached by input hash):
``num_workers=1``, a fixed ``random_seed`` and ``max_deterministic_time`` — a
work-based limit, never wall clock — make the solver reproducible for a given
OR-Tools build. Because a solver upgrade can change which solution comes back,
``ortools`` is pinned in ``requirements.txt`` and ``ENGINE_VERSION`` in
``search.py`` must be bumped whenever that pin moves.

"For a given build" is the exact scope of that guarantee, and the gap is
*measured*, not theoretical: on one audit cut list (pre-order 21 at kerf 5) the
macOS arm64 wheel and the manylinux x86_64 wheel of the SAME pinned version
return different — equally optimal — densest first boards, and the engine ends
at 5 boards on one and 6 on the other. Wherever the caller must pick one of
several tied optima, the pick is a property of the binary. Anything downstream
that depends on *which* optimum came back is therefore build-dependent; see
``tests/unit/test_cutting_search.py`` for how the benchmarks handle it.

``ortools`` is imported defensively: without it every entry point returns
``None`` and the engine degrades to the pure-heuristic Phase 1 behavior.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting.constructors import BinFill, PieceType, piece_type
from src.cutting.models import BinSpec, Cut, Piece, PlacedPiece, Rectangle
from src.cutting.parameters import CuttingParameters

try:  # pragma: no cover - exercised through ``is_available``
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - optional dependency
    cp_model = None


def is_available() -> bool:
    """``True`` when OR-Tools is installed and the exact endgame can run."""
    return cp_model is not None


# Column slots the model may open. Every extra slot costs variables and search
# symmetry; 40 covers a 2050mm board sliced into 50mm columns, well past
# anything a real cut list produces.
MAX_COLUMNS = 40


def _scale_for(values: Sequence[float]) -> int:
    """1 when every dimension is a whole millimetre, else 10 (0.1mm units).

    CP-SAT needs integers. Rounding is always *conservative* (pieces and kerf
    up, the bin down), so an integer-feasible solution is feasible in real
    coordinates too — never the other way round.
    """
    return 1 if all(float(v).is_integer() for v in values) else 10


class _Geometry:
    """Integer view of one bin + its pool, in scaled units."""

    def __init__(self, pool: Sequence[Piece], spec: BinSpec, params: CuttingParameters):
        self.left = max(0.0, params.left_trim)
        self.bottom = max(0.0, params.bottom_trim)
        usable_w = spec.width - self.left - max(0.0, params.right_trim)
        usable_h = spec.height - self.bottom - max(0.0, params.top_trim)
        kerf = max(0.0, params.kerf)

        dims: List[float] = [usable_w, usable_h, kerf]
        for piece in pool:
            dims.extend((piece.width, piece.height))
        self.scale = _scale_for(dims)

        self.W = math.floor(usable_w * self.scale)
        self.H = math.floor(usable_h * self.scale)
        self.K = math.ceil(kerf * self.scale)

    @property
    def valid(self) -> bool:
        return self.W > 0 and self.H > 0

    def up(self, value: float) -> int:
        return math.ceil(value * self.scale)

    def real(self, units: int) -> float:
        return units / self.scale


# One placement option of a piece type: (across, along, rotated) in scaled
# units. ``across`` sizes the column; ``along`` is consumed stacking it.
_Orientation = Tuple[int, int, bool]


def _orientations(
    piece: Piece, geo: _Geometry, transposed: bool, across_max: int, along_max: int
) -> List[_Orientation]:
    """Feasible orientations of ``piece``, deduped.

    ``transposed`` swaps which sheet axis the columns run along, so the same
    model expresses both first-stage directions (rip-first and crosscut-first).
    ``rotated`` keeps its domain meaning throughout: the piece's own width and
    height are swapped.
    """
    w, h = geo.up(piece.width), geo.up(piece.height)
    if transposed:
        options = [(h, w, False), (w, h, True)]
    else:
        options = [(w, h, False), (h, w, True)]
    if not piece.can_rotate or piece.width == piece.height:
        options = options[:1]
    out: List[_Orientation] = []
    seen = set()
    for across, along, rotated in options:
        if across > across_max or along > along_max:
            continue
        if (across, along) in seen:
            continue
        seen.add((across, along))
        out.append((across, along, rotated))
    return out


def _grouped(pool: Sequence[Piece]) -> List[Tuple[PieceType, List[Piece]]]:
    """Pool grouped by piece type in a stable order (the model's identity)."""
    groups: Dict[PieceType, List[Piece]] = {}
    for piece in sorted(pool, key=lambda p: p.id):
        groups.setdefault(piece_type(piece), []).append(piece)
    return sorted(groups.items())


def solve_bin(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    *,
    require_all: bool = True,
    transposed: bool = False,
    deterministic_time: float = 6.0,
    seed: int = 0,
    min_rect_size: float = 0.1,
    max_columns: int = MAX_COLUMNS,
) -> Optional[BinFill]:
    """Packs ``spec`` from ``pool`` with an exact 2-stage column model.

    ``require_all`` asks for a placement of the WHOLE pool (pure feasibility —
    the "does the tail close in one more board?" question); otherwise the solver
    maximizes placed area (orphan absorption).
    """
    if cp_model is None or not pool:
        return None
    geo = _Geometry(pool, spec, params)
    if not geo.valid:
        return None

    # Columns run along the sheet's height by default; ``transposed`` makes them
    # run along its width. Everything below is expressed in that local frame.
    across_max = geo.H if transposed else geo.W
    along_max = geo.W if transposed else geo.H

    groups = _grouped(pool)
    options_by_type = [
        _orientations(pieces[0], geo, transposed, across_max, along_max)
        for _t, pieces in groups
    ]
    if any(not options for options in options_by_type):
        # Some piece fits no orientation of this bin.
        if require_all:
            return None
        groups = [g for g, o in zip(groups, options_by_type) if o]
        options_by_type = [o for o in options_by_type if o]
        if not groups:
            return None

    narrowest = min(min(o[0] for o in options) for options in options_by_type)
    n_cols = max(1, min(max_columns, (across_max + geo.K) // (narrowest + geo.K)))

    model = cp_model.CpModel()
    widths = [model.NewIntVar(0, across_max, f"w{k}") for k in range(n_cols)]
    opened = [model.NewBoolVar(f"u{k}") for k in range(n_cols)]

    # counts[t][k][o] = instances of type t in column k under orientation o.
    # ``present`` mirrors "count > 0" — the column can only be sized off a
    # boolean, and it also links the column's ``opened`` flag.
    counts: List[List[List[object]]] = []
    for t_idx, (_t, pieces) in enumerate(groups):
        qty = len(pieces)
        per_column: List[List[object]] = []
        for k in range(n_cols):
            column: List[object] = []
            for o_idx, (across, _along, _rot) in enumerate(options_by_type[t_idx]):
                count = model.NewIntVar(0, qty, f"q{t_idx}_{k}_{o_idx}")
                present = model.NewBoolVar(f"b{t_idx}_{k}_{o_idx}")
                model.Add(count <= qty * present)
                model.Add(present <= opened[k])
                # A column is at least as wide as anything inside it.
                model.Add(widths[k] >= across * present)
                column.append(count)
            per_column.append(column)
        counts.append(per_column)

    for t_idx, (_t, pieces) in enumerate(groups):
        placed = [count for column in counts[t_idx] for count in column]
        if require_all:
            model.Add(sum(placed) == len(pieces))
        else:
            model.Add(sum(placed) <= len(pieces))

    for k in range(n_cols):
        # Stacking a column: n rows consume n lengths + (n-1) kerfs.
        stacked = [
            (along + geo.K) * counts[t_idx][k][o_idx]
            for t_idx in range(len(groups))
            for o_idx, (_across, along, _rot) in enumerate(options_by_type[t_idx])
        ]
        model.Add(sum(stacked) <= along_max + geo.K)
        model.Add(widths[k] == 0).OnlyEnforceIf(opened[k].Not())

    # Ripping the bin: m columns consume m widths + (m-1) kerfs.
    model.Add(sum(widths) + geo.K * sum(opened) <= across_max + geo.K)

    # Columns are interchangeable: force a canonical decreasing order.
    for k in range(n_cols - 1):
        model.Add(widths[k] >= widths[k + 1])
        model.AddImplication(opened[k + 1], opened[k])

    areas = [
        across * along * counts[t_idx][k][o_idx]
        for t_idx in range(len(groups))
        for k in range(n_cols)
        for o_idx, (across, along, _rot) in enumerate(options_by_type[t_idx])
    ]
    # Redundant but strong: placed area can never exceed the usable area.
    model.Add(sum(areas) <= across_max * along_max)
    if not require_all:
        model.Maximize(sum(areas))

    solver = cp_model.CpSolver()
    # Single worker + fixed seed + a WORK-based limit: the three together are
    # what make the solver reproducible. (``num_search_workers`` is the same
    # field under its old name — setting both rejects the parameters.)
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = deterministic_time
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    return _build_fill(
        solver=solver,
        groups=groups,
        options_by_type=options_by_type,
        counts=counts,
        widths=widths,
        opened=opened,
        geo=geo,
        spec=spec,
        n_cols=n_cols,
        transposed=transposed,
        across_max=across_max,
        along_max=along_max,
        min_rect_size=min_rect_size,
    )


def _build_fill(
    *,
    solver,
    groups: Sequence[Tuple[PieceType, List[Piece]]],
    options_by_type: Sequence[Sequence[_Orientation]],
    counts,
    widths,
    opened,
    geo: _Geometry,
    spec: BinSpec,
    n_cols: int,
    transposed: bool,
    across_max: int,
    along_max: int,
    min_rect_size: float,
) -> Optional[BinFill]:
    """Turns a solved column assignment into real placements, cuts and leftovers.

    Coordinates advance in *scaled integer* units and convert to millimetres
    only at the end, so the rounding slack (pieces up, bin down) always widens
    gaps: every separation is >= kerf by construction.
    """
    columns: List[Tuple[int, List[Tuple[int, int, bool, Piece]]]] = []
    cursors = [0] * len(groups)
    for k in range(n_cols):
        if not solver.Value(opened[k]):
            continue
        width = solver.Value(widths[k])
        rows: List[Tuple[int, int, bool, Piece]] = []
        for t_idx, (_t, pieces) in enumerate(groups):
            for o_idx, (across, along, rotated) in enumerate(options_by_type[t_idx]):
                for _ in range(solver.Value(counts[t_idx][k][o_idx])):
                    rows.append((along, across, rotated, pieces[cursors[t_idx]]))
                    cursors[t_idx] += 1
        if width <= 0 or not rows:
            continue
        rows.sort(key=lambda row: (-row[0], -row[1], row[3].id))
        columns.append((width, rows))

    if not columns:
        return None

    def point(along_pos: int, across_pos: int) -> Tuple[float, float]:
        """Column-local (along, across) -> sheet (x, y)."""
        if transposed:
            return geo.left + geo.real(along_pos), geo.bottom + geo.real(across_pos)
        return geo.left + geo.real(across_pos), geo.bottom + geo.real(along_pos)

    def rect(along_pos: int, across_pos: int, along: int, across: int) -> Rectangle:
        x, y = point(along_pos, across_pos)
        if transposed:
            return Rectangle(x, y, geo.real(along), geo.real(across))
        return Rectangle(x, y, geo.real(across), geo.real(along))

    placed: List[PlacedPiece] = []
    cuts: List[Cut] = []
    rects: List[Rectangle] = []

    def keep(candidate: Rectangle) -> None:
        if min(candidate.width, candidate.height) >= min_rect_size:
            rects.append(candidate)

    across_cursor = 0
    for width, rows in columns:
        along_cursor = 0
        for along, across, rotated, piece in rows:
            x, y = point(along_cursor, across_cursor)
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
            side = width - across
            if side > 0:
                # Third stage: trim the row down to the piece.
                sx, sy = point(along_cursor, across_cursor + across)
                cuts.append(Cut(sx, sy, geo.real(along), is_horizontal=transposed))
                if side > geo.K:
                    keep(
                        rect(
                            along_cursor,
                            across_cursor + across + geo.K,
                            along,
                            side - geo.K,
                        )
                    )
            along_cursor += along
            if along_cursor < along_max:
                # Second stage: crosscut separating this row from what follows.
                cx, cy = point(along_cursor, across_cursor)
                cuts.append(Cut(cx, cy, geo.real(width), is_horizontal=not transposed))
                along_cursor += geo.K
        if along_max - along_cursor > 0:
            keep(rect(along_cursor, across_cursor, along_max - along_cursor, width))
        across_cursor += width
        if across_cursor < across_max:
            # First stage: the rip separating this column from the rest.
            cx, cy = point(0, across_cursor)
            cuts.append(Cut(cx, cy, geo.real(along_max), is_horizontal=transposed))
            across_cursor += geo.K
    if across_max - across_cursor > 0:
        keep(rect(0, across_cursor, along_max, across_max - across_cursor))

    return BinFill(spec=spec, placed=placed, remainders=rects, cuts=cuts)


def fits_one_bin(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    *,
    deterministic_time: float = 6.0,
    seed: int = 0,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """Exact answer to "does this whole pool close in ONE ``spec``?".

    Tries both first-stage directions: rip-first and crosscut-first are
    genuinely different 2-stage families and a cut list rarely fits both.
    """
    for transposed in (False, True):
        fill = solve_bin(
            pool,
            spec,
            params,
            require_all=True,
            transposed=transposed,
            deterministic_time=deterministic_time,
            seed=seed,
            min_rect_size=min_rect_size,
        )
        if fill is not None and len(fill.placed) == len(pool):
            return fill
    return None
