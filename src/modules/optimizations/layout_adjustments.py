"""The seller's hand adjustments, laid over the optimizer's plan.

The engine produces a valid plan; now and then it is not the one the shop would
cut (an offcut shaped wrong, a sheet with two pieces on it, a half board that did
not come out). Rather than one more rule in the search for every such case, the
seller rearranges the plan by hand, and this module makes that safe.

What it holds on to:

- **A sheet is its placements.** A pool's adjustment (``LayoutAdjustment``) says,
  per sheet, which stock it is and where each piece instance sits — nothing
  else. Cuts, leftovers, edges, statistics, the materials summary and the money
  are all DERIVED, by the same functions that derive them for the engine's own
  sheets (``sheet_check.realize_sheet`` wraps ``consolidate``; the service's
  ``_serialize_layout`` is the one ``_build_result_payload`` uses). There is no
  second representation of a plan to drift away from the first.
- **An adjustment replaces its pool whole** instead of recording a diff against
  the engine's output. It is self-contained, so an ``ENGINE_VERSION`` bump or a
  price change cannot make it point at pieces that moved; and when the cut list
  itself changes, it simply stops holding and is dropped, never reinterpreted.
- **It is applied after the cache**, exactly like ``apply_whole_boards``: the
  optimization hash, the cached payload and ``ENGINE_VERSION`` are untouched, and
  a quote without adjustments comes out byte-identical to what it was.

Three modes, because the same adjustment is read in three situations:

- ``lenient`` — every read (the quote, the review link, the order). A pool
  whose adjustment no longer holds falls back to the engine's sheets and the
  reason is reported. Never raises: a pre-order re-validates on every read, and
  an error there would surface as a 500 on a quote that used to open.
- ``strict`` — a write. Anything wrong is refused, pending pieces included.
- ``editing`` — the layout editor. Violations are refused, but pieces may sit
  in the pending tray while the seller moves things around.

Pending pieces are legitimate only on a pool of finite offcuts, the one place
the engine itself reports ``unplaced``; a pool with a catalog board can always
open another sheet, so a pending piece there means an unfinished edit.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cutting.models import (
    CuttingLayout,
    Material,
    Piece,
    PlacedPiece,
    Rectangle,
)
from src.cutting.parameters import CuttingParameters
from src.cutting.sheet_check import (
    INSIDE_TRIM,
    KERF,
    NOT_GUILLOTINE,
    OUT_OF_BOUNDS,
    OVERLAP,
    ROTATION_NOT_ALLOWED,
    UNVERIFIABLE,
    WHOLE_OFFCUT_OVERLAP,
    WRONG_DIMENSIONS,
    realize_sheet,
)
from src.modules.optimizations.patterns import base_label, group_layouts, order_sheets
from src.modules.optimizations.schemas import (
    AdjustedPiece,
    AdjustedSheet,
    LayoutAdjustment,
    WholeOffcut,
)
from src.modules.optimizations.summary import build_materials_summary
from src.shared.exceptions import ValidationError

LENIENT = "lenient"
STRICT = "strict"
EDITING = "editing"

# Reasons that belong to the adjustment rather than to one sheet's geometry.
UNKNOWN_POOL = "unknown_pool"
DUPLICATE_POOL = "duplicate_pool"
SHEET_NOT_AVAILABLE = "sheet_not_available"
OFFCUT_EXHAUSTED = "offcut_exhausted"
UNKNOWN_PIECE = "unknown_piece"
DUPLICATE_PIECE = "duplicate_piece"
PENDING_PIECES = "pending_pieces"


@dataclass(frozen=True)
class SheetBin:
    """One kind of sheet a pool may be cut on, as the engine would see it."""

    material_key: str
    half_board: bool
    width: float
    height: float
    thickness: float
    cost_per_unit: float
    # Units available; ``None`` = unlimited (a catalog board, a manual measure).
    count: Optional[int]
    label: str

    def material(self) -> Material:
        return Material(
            id=self.material_key,
            width=self.width,
            height=self.height,
            thickness=self.thickness,
            cost_per_unit=self.cost_per_unit,
            half_board=self.half_board,
        )


@dataclass
class PoolSpec:
    """Everything needed to check and realize one pool's sheets.

    Built by the service from the very objects ``compute`` hands the engine —
    the instance ids of ``_build_pieces``, the pool's ``CuttingParameters``
    (trims off when the anchor says ``skipTrim``), ``_half_spec`` — so a sheet is
    held to exactly the constraints the search packed it under.
    """

    pool_key: str
    label: str
    # Instance id -> piece, in cut-list order.
    pieces: Dict[str, Piece]
    params: CuttingParameters
    # (material_key, half_board) -> bin; the anchor first.
    bins: Dict[Tuple[str, bool], SheetBin]
    # The anchor is a finite offcut: pieces may legitimately stay unplaced.
    finite: bool
    serialize: Callable[[CuttingLayout], dict]
    min_usable_offcut: float

    @property
    def material_keys(self) -> set:
        return {key for key, _ in self.bins}


@dataclass
class Issue:
    pool_key: str
    code: str
    message: str
    sheet_index: Optional[int] = None
    piece_ids: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "pool_key": self.pool_key,
            "code": self.code,
            "message": self.message,
            "sheet_index": self.sheet_index,
            "piece_ids": list(self.piece_ids),
        }


@dataclass
class RealizedPool:
    """One pool's sheets, realized. ``issues`` empty means it holds."""

    # (working sheet, its serialized layout or None when empty), in the order
    # the adjustment listed them — the editor's indices stay put.
    entries: List[Tuple[AdjustedSheet, Optional[dict]]] = field(default_factory=list)
    pending: List[Piece] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    @property
    def layouts(self) -> List[dict]:
        """The non-empty sheets in the order the shop cuts them (half last)."""
        return order_sheets(
            [layout for _, layout in self.entries if layout is not None]
        )


class LayoutAdjustmentError(ValidationError):
    """A hand adjustment that does not hold, refused on a write or an edit."""

    def __init__(self, issues: Sequence[Issue]):
        self.issues = list(issues)
        first = self.issues[0].message
        more = len(self.issues) - 1
        detail = first if not more else f"{first} (y {more} problema(s) más)"
        super().__init__(detail, field="layoutAdjustments")


def _sheet_label(spec: PoolSpec, index: Optional[int]) -> str:
    return f"la hoja {index + 1} de {spec.label}" if index is not None else spec.label


def describe(
    code: str,
    spec: PoolSpec,
    sheet_index: Optional[int] = None,
    piece_ids: Sequence[str] = (),
    extra: str = "",
) -> str:
    """The seller-facing reason, in Spanish. One place, so the words agree."""
    where = _sheet_label(spec, sheet_index)
    a = f"«{piece_ids[0]}»" if piece_ids else "Una pieza"
    b = f"«{piece_ids[1]}»" if len(piece_ids) > 1 else "otra pieza"
    kerf = f"{spec.params.kerf:g}"
    messages = {
        OUT_OF_BOUNDS: f"{a} se sale de {where}.",
        INSIDE_TRIM: f"{a} invade el refilado de {where}.",
        ROTATION_NOT_ALLOWED: f"{a} no se puede rotar: tiene veta fija.",
        WRONG_DIMENSIONS: f"{a} no tiene las medidas de su pieza en {where}.",
        OVERLAP: f"{a} se cruza con {b} en {where}.",
        KERF: (
            f"{a} y {b} quedan a menos de un corte de sierra ({kerf} mm) "
            f"en {where}."
        ),
        WHOLE_OFFCUT_OVERLAP: (
            f"{a} cae sobre un retazo que debe salir entero en {where}."
            if piece_ids
            else f"Un retazo entero de {where} se sale del área útil o se cruza con otro."
        ),
        NOT_GUILLOTINE: f"{where[0].upper()}{where[1:]} no se puede cortar con cortes de lado a lado.",
        UNVERIFIABLE: (
            f"{where[0].upper()}{where[1:]} es demasiado compleja para verificarla; "
            "simplifica la distribución."
        ),
        UNKNOWN_POOL: f"El material {spec.label} ya no está en la cotización.",
        DUPLICATE_POOL: f"El ajuste de {spec.label} está repetido.",
        SHEET_NOT_AVAILABLE: f"{where[0].upper()}{where[1:]} usa un tablero que ya no está disponible.",
        OFFCUT_EXHAUSTED: f"Se usan más retazos {extra} de los disponibles.",
        UNKNOWN_PIECE: f"{a} ya no está en el despiece de {spec.label}.",
        DUPLICATE_PIECE: f"{a} está ubicada dos veces en {spec.label}.",
        PENDING_PIECES: f"Quedan piezas sin ubicar en {spec.label}: {extra}.",
    }
    return messages.get(code, f"El ajuste de {where} no es válido ({code}).")


def _issue(
    spec: PoolSpec,
    code: str,
    sheet_index: Optional[int] = None,
    piece_ids: Sequence[str] = (),
    extra: str = "",
) -> Issue:
    return Issue(
        pool_key=spec.pool_key,
        code=code,
        message=describe(code, spec, sheet_index, piece_ids, extra),
        sheet_index=sheet_index,
        piece_ids=tuple(piece_ids),
    )


def _placed_signature(material_key, half_board, placements) -> tuple:
    return (
        material_key,
        bool(half_board),
        tuple(
            sorted((pid, float(x), float(y), bool(r)) for pid, x, y, r in placements)
        ),
    )


def _layout_signature(layout: dict) -> tuple:
    material = layout.get("material") or {}
    return _placed_signature(
        material.get("material_key"),
        material.get("half_board", False),
        [
            (p.get("piece_id"), p.get("x"), p.get("y"), p.get("rotated", False))
            for p in layout.get("placed_pieces") or []
        ],
    )


def sheets_from_layouts(layouts: Iterable[dict]) -> List[AdjustedSheet]:
    """The engine's sheets for a pool, written as an adjustment.

    What the editor starts from when a pool has no adjustment yet; also what
    makes "an adjustment identical to the plan" a no-op.
    """
    return [
        AdjustedSheet(
            material_key=(layout.get("material") or {}).get("material_key"),
            half_board=bool((layout.get("material") or {}).get("half_board", False)),
            pieces=[
                AdjustedPiece(
                    piece_id=str(p.get("piece_id")),
                    x=p.get("x"),
                    y=p.get("y"),
                    rotated=bool(p.get("rotated", False)),
                )
                for p in layout.get("placed_pieces") or []
            ],
            whole_offcuts=[
                WholeOffcut(x=r["x"], y=r["y"], width=r["width"], height=r["height"])
                for r in layout.get("remainders") or []
                if r.get("kept_whole")
            ],
        )
        for layout in layouts
    ]


def placed_pieces(sheet: AdjustedSheet, spec: PoolSpec) -> List[PlacedPiece]:
    """The sheet's pieces as the engine's ``PlacedPiece``; unknown ids skipped."""
    placed = []
    for ap in sheet.pieces:
        piece = spec.pieces.get(ap.piece_id)
        if piece is None:
            continue
        w, h = (
            (piece.height, piece.width) if ap.rotated else (piece.width, piece.height)
        )
        placed.append(
            PlacedPiece(
                piece=piece,
                x=float(ap.x),
                y=float(ap.y),
                width=w,
                height=h,
                rotated=bool(ap.rotated),
            )
        )
    return placed


def whole_offcut_rects(sheet: AdjustedSheet) -> List[Rectangle]:
    return [
        Rectangle(float(r.x), float(r.y), float(r.width), float(r.height))
        for r in sheet.whole_offcuts
    ]


def realize_pool(
    adjustment: LayoutAdjustment,
    spec: PoolSpec,
    base_layouts: Sequence[dict],
    mode: str,
) -> RealizedPool:
    """Checks and realizes every sheet of one pool's adjustment.

    Collects every problem rather than stopping at the first, so the seller sees
    them all. A sheet identical to one the engine produced reuses that layout
    verbatim: re-deriving it could pick another, equally good cut tree and show
    a change nobody made.
    """
    result = RealizedPool()
    base_by_signature: Dict[tuple, List[dict]] = {}
    for layout in base_layouts:
        if not any(r.get("kept_whole") for r in layout.get("remainders") or []):
            base_by_signature.setdefault(_layout_signature(layout), []).append(layout)
    base_sheet_of: Dict[str, int] = {}
    base_position: Dict[str, tuple] = {}
    ordinal_of = {id(layout): i for i, layout in enumerate(base_layouts)}
    for ordinal, layout in enumerate(base_layouts):
        for p in layout.get("placed_pieces") or []:
            pid = str(p.get("piece_id"))
            base_sheet_of[pid] = ordinal
            base_position[pid] = (p.get("x"), p.get("y"), bool(p.get("rotated", False)))

    used: Counter = Counter()
    seen_pieces: set = set()
    matched_base: set = set()
    for index, sheet in enumerate(adjustment.sheets):
        sheet_bin = spec.bins.get((sheet.material_key, sheet.half_board))
        if sheet_bin is None:
            result.issues.append(_issue(spec, SHEET_NOT_AVAILABLE, index))
            continue
        if not sheet.pieces:
            # An empty sheet is never cut nor billed; the editor keeps it so a
            # piece can be dropped on it.
            result.entries.append((sheet, None))
            continue
        used[sheet_bin.material_key] += 1
        if (
            sheet_bin.count is not None
            and used[sheet_bin.material_key] > sheet_bin.count
        ):
            result.issues.append(
                _issue(spec, OFFCUT_EXHAUSTED, index, extra=f"«{sheet_bin.label}»")
            )
            continue
        clean = True
        for ap in sheet.pieces:
            if ap.piece_id not in spec.pieces:
                result.issues.append(_issue(spec, UNKNOWN_PIECE, index, (ap.piece_id,)))
                clean = False
            elif ap.piece_id in seen_pieces:
                result.issues.append(
                    _issue(spec, DUPLICATE_PIECE, index, (ap.piece_id,))
                )
                clean = False
            seen_pieces.add(ap.piece_id)
        if not clean:
            continue

        placed = placed_pieces(sheet, spec)
        whole = whole_offcut_rects(sheet)
        signature = _placed_signature(
            sheet_bin.material_key,
            sheet_bin.half_board,
            [(pp.piece.id, pp.x, pp.y, pp.rotated) for pp in placed],
        )
        reusable = base_by_signature.get(signature) if not whole else None
        if reusable:
            base = reusable.pop(0)
            matched_base.add(ordinal_of[id(base)])
            layout_dict = {**base, "material": dict(base["material"])}
        else:
            layout, violations = realize_sheet(
                sheet_bin.material(),
                placed,
                spec.params,
                whole,
                min_usable_offcut=spec.min_usable_offcut,
            )
            if violations:
                result.issues.extend(
                    _issue(spec, v.code, index, v.piece_ids) for v in violations
                )
                continue
            layout_dict = spec.serialize(layout)
            whole_boxes = {(r.x, r.y, r.width, r.height) for r in whole}
            for r in layout_dict.get("remainders") or []:
                if (r["x"], r["y"], r["width"], r["height"]) in whole_boxes:
                    r["kept_whole"] = True
            layout_dict["adjusted"] = True
            _mark_moved(layout_dict, base_sheet_of, base_position, matched_base)
        result.entries.append((sheet, layout_dict))

    result.pending = [p for pid, p in spec.pieces.items() if pid not in seen_pieces]
    # Only on an otherwise sound pool: the pieces of a sheet refused above would
    # all read as "pending" too, which is noise on top of the real reason.
    if result.pending and not result.issues and not spec.finite and mode != EDITING:
        result.issues.append(
            _issue(
                spec,
                PENDING_PIECES,
                extra=", ".join(f"«{p.id}»" for p in result.pending[:5])
                + (" …" if len(result.pending) > 5 else ""),
            )
        )
    _number_sheets(result.layouts)
    return result


def _mark_moved(
    layout: dict,
    base_sheet_of: Dict[str, int],
    base_position: Dict[str, tuple],
    matched_base: set,
) -> None:
    """Flags the pieces that are not where the optimizer left them.

    "Where" includes the sheet: an adjusted sheet is matched to the engine sheet
    most of its pieces came from (each engine sheet matched once), and a piece
    counts as moved when it came from another sheet or sits elsewhere on its own.
    """
    votes = Counter(
        base_sheet_of[pid]
        for pid in (str(p.get("piece_id")) for p in layout.get("placed_pieces") or [])
        if pid in base_sheet_of and base_sheet_of[pid] not in matched_base
    )
    home = min(votes, key=lambda o: (-votes[o], o)) if votes else None
    if home is not None:
        matched_base.add(home)
    for p in layout.get("placed_pieces") or []:
        pid = str(p.get("piece_id"))
        same_sheet = home is not None and base_sheet_of.get(pid) == home
        same_spot = base_position.get(pid) == (
            p.get("x"),
            p.get("y"),
            bool(p.get("rotated", False)),
        )
        if not (same_sheet and same_spot):
            p["adjusted"] = True


def _number_sheets(layouts: Sequence[dict]) -> None:
    """1..n per material key, in cutting order — the engine's own convention."""
    counter: Counter = Counter()
    for layout in layouts:
        key = layout["material"].get("material_key")
        counter[key] += 1
        layout["material"]["sheet_number"] = counter[key]


def _group_pending(pool_key: str, pending: Sequence[Piece]) -> List[dict]:
    """Pending pieces as ``unplaced`` entries, grouped the way the engine groups."""
    grouped: Dict[tuple, dict] = {}
    for piece in pending:
        label = base_label(piece.id)
        key = (label, piece.width, piece.height)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "material_key": pool_key,
                "label": label,
                "width": piece.width,
                "height": piece.height,
                "quantity": 1,
            }
        else:
            entry["quantity"] += 1
    return list(grouped.values())


def realize_adjustments(
    adjustments: Sequence[LayoutAdjustment],
    specs: Dict[str, PoolSpec],
    base_by_pool: Dict[str, List[dict]],
    mode: str,
) -> Tuple[Dict[str, RealizedPool], List[Issue]]:
    """Realizes every pool's adjustment; ``(applied pools, what did not hold)``.

    In ``lenient`` mode a pool that does not hold is left out and reported; in
    the other two anything wrong raises ``LayoutAdjustmentError``.
    ``base_by_pool`` (the engine's sheets per pool) only lets an untouched sheet
    be reused verbatim; a check with no plan at hand passes ``{}``.
    """
    realized: Dict[str, RealizedPool] = {}
    issues: List[Issue] = []
    seen: set = set()
    for adjustment in adjustments:
        spec = specs.get(adjustment.pool_key)
        if spec is None:
            issues.append(
                Issue(
                    pool_key=adjustment.pool_key,
                    code=UNKNOWN_POOL,
                    message=(
                        f"El material «{adjustment.pool_key}» ya no está en la "
                        "cotización."
                    ),
                )
            )
            continue
        if adjustment.pool_key in seen:
            issues.append(_issue(spec, DUPLICATE_POOL))
            continue
        seen.add(adjustment.pool_key)
        pool = realize_pool(adjustment, spec, base_by_pool.get(spec.pool_key, []), mode)
        if pool.issues:
            issues.extend(pool.issues)
            continue
        realized[spec.pool_key] = pool
    if issues and mode != LENIENT:
        raise LayoutAdjustmentError(issues)
    return realized, issues


def group_by_pool(
    layouts: Iterable[dict], pool_order: Sequence[str], pool_of: Dict[str, str]
) -> Dict[str, List[dict]]:
    """The payload's sheets, per pool, in the order the payload lists them."""
    grouped: Dict[str, List[dict]] = {key: [] for key in pool_order}
    for layout in layouts:
        key = pool_of.get((layout.get("material") or {}).get("material_key"))
        grouped.setdefault(key, []).append(layout)
    return grouped


def apply_layout_adjustments(
    payload: dict,
    adjustments: Optional[Sequence[LayoutAdjustment]],
    specs: Dict[str, PoolSpec],
    pool_order: Sequence[str],
    pool_of: Dict[str, str],
    finite_keys: set,
    mode: str,
) -> Tuple[dict, Dict[str, RealizedPool], List[Issue]]:
    """Lays the adjustments over the engine's payload.

    Returns ``(payload, realized, issues)``: ``realized`` holds the pools that
    were applied, ``issues`` everything that was not (see
    ``realize_adjustments`` for what raises). With nothing to apply the payload
    comes back as the very same object.
    """
    if not adjustments:
        return payload, {}, []

    base_by_pool = group_by_pool(payload.get("layouts") or [], pool_order, pool_of)
    realized, issues = realize_adjustments(adjustments, specs, base_by_pool, mode)
    if not realized:
        return ({**payload, "layout_issues": [i.to_dict() for i in issues]}, {}, issues)

    layouts: List[dict] = []
    for key in pool_order:
        layouts.extend(
            realized[key].layouts if key in realized else base_by_pool.get(key, [])
        )
    unplaced = [
        entry
        for entry in payload.get("unplaced") or []
        if entry.get("material_key") not in realized
    ]
    for key, pool in realized.items():
        unplaced.extend(_group_pending(key, pool.pending))

    result = dict(payload)
    result["layouts"] = layouts
    result["unplaced"] = unplaced
    result["total_boards_used"] = sum(
        1
        for layout in layouts
        if layout["material"].get("material_key") not in finite_keys
    )
    result["total_boards_cost"] = sum(
        layout["material"].get("cost_per_unit", 0.0) for layout in layouts
    )
    result["total_cut_linear_m"] = round(
        sum(
            (layout.get("statistics") or {}).get("cut_linear_m", 0.0)
            for layout in layouts
        ),
        2,
    )
    result["total_edge_banding_linear_m"] = round(
        sum(
            (layout.get("statistics") or {}).get("edge_banding_linear_m", 0.0)
            for layout in layouts
        ),
        2,
    )
    result["materials_summary"] = build_materials_summary(
        layouts, payload.get("materials") or []
    )
    result["layout_groups"] = group_layouts(layouts)
    # What was applied, normalized: the sheets that exist. It is what an order
    # freezes and what salts the plan's hash, so an empty sheet the editor kept
    # around must not make two identical plans look different.
    result["layout_adjustments"] = [
        {
            **adjustment.model_dump(mode="json", exclude={"sheets"}),
            "sheets": [
                sheet.model_dump(mode="json")
                for sheet in adjustment.sheets
                if sheet.pieces
            ],
        }
        for adjustment in adjustments
        if adjustment.pool_key in realized
    ]
    if issues:
        result["layout_issues"] = [i.to_dict() for i in issues]
    return result, realized, issues
