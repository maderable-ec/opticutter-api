"""Pieces the plan does not cut: why each one is left out, and the write gate.

``unplaced`` started as information: a pool of finite retazos can run out, and
the seller was told which pieces the plan leaves out, to add a retazo, attach
a board or drop them. Pre-order 157 showed what that is worth on a quote.
Five pieces 5 mm taller than the useful area of their board went out to the
client, with a yellow warning on screen that nobody read. Had it been
confirmed, the order would have been frozen without them.

So a plan with a piece left out is now an error on every write (creating or
saving a quote, sending its link, minting the order). It stays information only
where the seller is still working: ``/optimize``, the layout editor and the
drafts. Reads never refuse, because a pre-order re-validates on every read and an
error there is a quote that no longer opens.

Everything here is pure: the service hands over, per pool, the few numbers that
decide whether a piece fits (``PoolGeometry``), built from the very objects the
search was packed under.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src.shared.exceptions import BusinessRuleError


class UnplacedReason(str, Enum):
    # It fits some sheet of the pool, but the pool ran out: only a finite one can.
    out_of_stock = "out_of_stock"
    # It fits, but the seller's hand adjustment of the pool left it on no sheet.
    pending = "pending"
    # Larger than the useful area, but not than the sheet: it fits untrimmed.
    larger_than_trimmed_sheet = "larger_than_trimmed_sheet"
    # Larger than every sheet of the pool, trims or not.
    larger_than_sheet = "larger_than_sheet"


class UnplacedPiecesError(BusinessRuleError):
    """A write whose plan leaves a piece uncut. Its own code so a client can tell."""

    code = "UNPLACED_PIECES"


@dataclass(frozen=True)
class PoolGeometry:
    """What decides whether a piece fits one pool."""

    # How the seller names the pool's anchor.
    name: str
    # ``(height, width)`` of every sheet the pool may cut from, the anchor first.
    sheets: Tuple[Tuple[float, float], ...]
    # ``(top, bottom, left, right)`` as the pool is cut: zeros under ``skipTrim``.
    trims: Tuple[float, float, float, float]
    # ``(height, width)`` of the pieces of this pool that may be turned.
    rotatable: FrozenSet[Tuple[float, float]]
    # A hand adjustment was laid over this pool: whatever it left out and fits is
    # a piece the seller has not placed yet, not material that ran out.
    adjusted: bool = False


def _fits(
    height: float, width: float, sheet: Tuple[float, float], rotate: bool
) -> bool:
    # No kerf: the packer only adds it when something is left over beside the
    # piece, so a piece exactly the size of the useful area is placed.
    sheet_height, sheet_width = sheet
    if height <= sheet_height and width <= sheet_width:
        return True
    return rotate and width <= sheet_height and height <= sheet_width


def explain_unplaced(
    unplaced: Sequence[dict], pools: Mapping[str, PoolGeometry]
) -> List[dict]:
    """Each unplaced group plus its material's name, useful area and ``reason``.

    The useful area is the anchor's, which is the sheet the seller picked. A
    group whose pool is unknown is returned as it came.
    """
    out: List[dict] = []
    for entry in unplaced:
        pool = pools.get(entry["material_key"])
        if pool is None:
            out.append(dict(entry))
            continue
        top, bottom, left, right = pool.trims
        useful = [(h - top - bottom, w - left - right) for h, w in pool.sheets]
        height, width = entry["height"], entry["width"]
        rotate = (height, width) in pool.rotatable
        if any(_fits(height, width, sheet, rotate) for sheet in useful):
            reason = (
                UnplacedReason.pending if pool.adjusted else UnplacedReason.out_of_stock
            )
        elif any(pool.trims) and any(
            _fits(height, width, sheet, rotate) for sheet in pool.sheets
        ):
            reason = UnplacedReason.larger_than_trimmed_sheet
        else:
            reason = UnplacedReason.larger_than_sheet
        out.append(
            {
                **entry,
                "material_name": pool.name,
                "usable_height": useful[0][0],
                "usable_width": useful[0][1],
                "reason": reason.value,
            }
        )
    return out


def format_mm(value: float) -> str:
    """A measurement as the shop writes it: ``2785``, not ``2785.0``."""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _seller_label(label: Optional[str]) -> Optional[str]:
    # ``piece_N`` is the id the optimizer invents for an unlabeled row, numbered
    # inside its material group: naming it would send the seller looking for a
    # "piece_3" that no screen shows.
    if not label or re.fullmatch(r"piece_\d+", label):
        return None
    return label


def _join(items: List[str]) -> str:
    """``a``, ``a y b``, ``a, b y c``."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} y {items[-1]}"


def unplaced_message(explained: Sequence[dict]) -> str:
    """One message for the whole plan, grouped by material and reason.

    One, because the web shows the first message of the error envelope and
    nothing else: it has to name every piece, the board and why on its own.
    """
    total = sum(entry["quantity"] for entry in explained)
    clauses: Dict[tuple, List[dict]] = {}
    for entry in explained:
        key = (entry["material_key"], entry.get("reason"))
        clauses.setdefault(key, []).append(entry)

    parts = []
    for (_, reason), entries in clauses.items():
        pieces = []
        for entry in entries:
            label = _seller_label(entry.get("label"))
            named = f" «{label}»" if label else ""
            pieces.append(
                f"{entry['quantity']}{named} de "
                f"{format_mm(entry['height'])}×{format_mm(entry['width'])} mm"
            )
        clause = _join(pieces)
        first = entries[0]
        if first.get("material_name"):
            clause += f" en {first['material_name']}"
        count = sum(entry["quantity"] for entry in entries)
        if reason == UnplacedReason.out_of_stock.value:
            clause += (
                " (no alcanza el material: agrega retazos o un tablero de catálogo "
                "al grupo)"
            )
        elif reason == UnplacedReason.pending.value:
            left, place = (
                ("quedó", "colócala") if count == 1 else ("quedaron", "colócalas")
            )
            clause += (
                f" ({left} fuera del ajuste manual: {place} en una hoja o descarta "
                "el ajuste)"
            )
        elif reason is not None:
            size = (
                f"{format_mm(first['usable_height'])}×"
                f"{format_mm(first['usable_width'])}"
            )
            useful = f"área útil {size} mm"
            if reason == UnplacedReason.larger_than_trimmed_sheet.value:
                verb = "entraría" if count == 1 else "entrarían"
                useful += f"; {verb} sin refilar"
            clause += f" ({useful})"
        parts.append(clause)

    head = (
        "Hay 1 pieza que no entra en el material y quedaría sin cortar"
        if total == 1
        else f"Hay {total} piezas que no entran en el material y quedarían sin cortar"
    )
    return (
        f"{head}: {'; '.join(parts)}. "
        "Corrige las medidas, cambia el material o quita esas piezas."
    )
