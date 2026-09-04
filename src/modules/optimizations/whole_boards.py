"""Half board promoted to a whole board, on top of an already-optimized payload.

Pure layer (no DB, no framework, no ``src.cutting``): the cut geometry is cached
by hash and this flag is **not** in that hash, so — exactly like the price level
in ``price_levels.py`` — the decision is applied **after** ``compute()`` as a
deterministic transform, keeping one cache entry per cut plan. It runs **after**
the level, which has already set each material's ``cost_per_unit`` to what the
whole sheet is billed at.

Business rule (decided with the user): the optimizer bills a sheet as a half
board whenever the content re-packs into ``width/2``, but the client may want
the whole sheet anyway (and take the leftover home). Marking the board
(``wholeBoard`` on the material input) does **not** re-run the search: re-doing
it without the half spec would let the beam re-partition the whole pool, moving
pieces the client already approved and shattering the leftover into fragments —
without ever costing less, since N whole boards would already have beaten
N + ½ on price. Promoting instead keeps every placement byte-identical and
turns the uncut half into **one** clean remainder.

What this module is NOT: it never re-packs. The deleted ``half_boards.py``
called the packer post-hoc and died for it when the half board became a
first-class bin. Here the geometry is already decided; this only re-labels,
re-prices and hands back the half the client is paying for.
"""

from typing import Dict, Iterable, List, Optional, Tuple

from src.modules.optimizations.patterns import group_layouts
from src.modules.optimizations.schemas import MaterialSource
from src.modules.optimizations.summary import build_materials_summary


def apply_whole_boards(payload: dict, whole_board_keys: Iterable[str]) -> dict:
    """Promotes every marked half-board sheet to its whole board.

    ``whole_board_keys`` are the material keys the seller marked
    (``OptimizeRequest.whole_board_material_keys``). Returns the payload
    **unchanged (same object)** when there is nothing to promote — the normal
    case — and never mutates the input otherwise: the promoted sheets are
    rebuilt, everything else is reused by reference.
    """
    keys = set(whole_board_keys or ())
    if not keys:
        return payload

    materials = payload.get("materials") or []
    materials_by_key: Dict[str, dict] = {m.get("material_key"): m for m in materials}

    layouts: List[dict] = []
    cost_delta = 0.0
    cut_delta = 0.0
    promoted_any = False

    for layout in payload.get("layouts") or []:
        promoted = _promote(layout, keys, materials_by_key)
        if promoted is None:
            layouts.append(layout)
            continue
        promoted_any = True
        new_layout, sheet_cost_delta, sheet_cut_delta = promoted
        layouts.append(new_layout)
        cost_delta += sheet_cost_delta
        cut_delta += sheet_cut_delta

    if not promoted_any:
        return payload

    result = dict(payload)
    result["layouts"] = layouts
    # A delta, not a re-sum: it keeps this pass independent of how the base
    # total was composed. Safe because a half board is always a catalog sheet,
    # never an offcut (``_half_spec`` returns None for non-catalog materials).
    result["total_boards_cost"] = round(
        payload.get("total_boards_cost", 0.0) + cost_delta, 2
    )
    result["total_cut_linear_m"] = round(
        payload.get("total_cut_linear_m", 0.0) + cut_delta, 2
    )
    # ``total_boards_used`` is deliberately untouched: a half already counted as
    # one board.
    result["materials_summary"] = build_materials_summary(layouts, materials)
    # Mandatory, not cosmetic: on the cold path ``layout_groups[i]["layout"]``
    # IS the same object as ``layouts[i]``, but after the cache's JSON round
    # trip they are separate copies — without rebuilding, the diagram (which
    # reads ``layout_groups``) would show half a sheet on a cache hit and a
    # whole one on a cold compute. Signatures ignore width and ``half_board``,
    # so the pattern ids and sheet numbers come back identical.
    result["layout_groups"] = group_layouts(layouts)
    return result


def _promote(
    layout: dict, keys: set, materials_by_key: Dict[str, dict]
) -> Optional[Tuple[dict, float, float]]:
    """Returns ``(promoted_layout, cost_delta, cut_delta)`` or ``None``.

    ``None`` means "leave this sheet alone": it isn't a half board, isn't
    marked, or its material isn't a catalog board.
    """
    material = layout.get("material") or {}
    key = material.get("material_key")
    if not material.get("half_board") or key not in keys:
        return None

    full = materials_by_key.get(key)
    if full is None or full.get("source") != MaterialSource.catalog.value:
        return None

    half_width = material.get("width", 0.0)
    full_width = full.get("width", 0.0)
    height = material.get("height", 0.0)
    if full_width <= half_width or height <= 0:
        return None

    full_area = full_width * height
    full_cost = full.get("cost_per_unit", 0.0)

    statistics = layout.get("statistics") or {}
    used_area = statistics.get("used_area", 0.0)
    rip_linear_m = round(height / 1000.0, 2)

    new_layout = dict(layout)
    new_layout["material"] = {
        **material,
        "width": full_width,
        "area": full_area,
        "cost_per_unit": full_cost,
        "half_board": False,
    }
    # ``placed_pieces`` is carried over BY REFERENCE: promoting a board must be
    # incapable of moving a piece, and sharing the list makes that structural.
    new_layout["remainders"] = [
        *(layout.get("remainders") or []),
        # The uncut half, as one clean rectangle. Width as a subtraction (not
        # ``half_width``) so it closes on the board's edge exactly, whatever
        # rounding the half spec did. Like ``_half_spec``, it ignores the kerf
        # the rip actually eats — staying consistent with what is billed.
        {
            "x": half_width,
            "y": 0.0,
            "width": full_width - half_width,
            "height": height,
        },
    ]
    new_layout["cuts"] = [
        # First, because it is the first thing the operator does: the sheet is
        # ripped in two before the half gets broken down.
        {"x": half_width, "y": 0.0, "length": height, "is_horizontal": False},
        *(layout.get("cuts") or []),
    ]
    new_layout["statistics"] = {
        **statistics,
        "waste_area": full_area - used_area,
        # The delivered half counts as waste: the client bought a whole board
        # and used half of it. Honest, and it is what the labelled leftover on
        # the diagram explains.
        "efficiency": round(used_area / full_area * 100, 2) if full_area else 0.0,
        "cut_linear_m": round(statistics.get("cut_linear_m", 0.0) + rip_linear_m, 2),
    }

    return new_layout, full_cost - material.get("cost_per_unit", 0.0), rip_linear_m
