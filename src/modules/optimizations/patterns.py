"""Grouping of layouts by cutting pattern.

Multiple sheets can share the exact same cut arrangement (same geometry and
same piece labels). These utilities detect those repeated patterns to
deduplicate the visual representation and the summary, without altering the
physical board count.
"""

import re
from typing import List, Sequence, Tuple, TypeVar

SheetT = TypeVar("SheetT")

# The optimizer expands pieces with an instance suffix ``#{i+1}`` when
# ``quantity > 1`` (see ``src/cutting/optimizer.py``). To compare patterns we
# care about the base label, not the specific instance. ``#`` (not ``_``) is used
# so labels that already end in ``_<n>`` (e.g. the auto-label ``piece_1`` or a
# user label ``estante_2``) aren't mangled.
_INSTANCE_SUFFIX = re.compile(r"#\d+$")


def order_sheets(layouts: Sequence[SheetT]) -> List[SheetT]:
    """Whole boards first, the half board last.

    The engine has no opinion here: ``half_downgrade`` swaps a fill IN PLACE
    (``fills[i] = swapped``), so a half board keeps whatever position the whole
    board it replaced happened to hold and shows up interleaved. The shop cuts
    the whole boards first and the half last, so that is the order the payload
    carries — the optimizer's diagram, the client review, the production PDF
    and the workshop board all read the same list, so ordering it once here
    beats four copies of the rule.

    Stable, so the order the search decided survives within each half. Applied
    per material pool, never across the whole job: two materials must stay in
    blocks.

    Duck-typed on ``.material.half_board`` so it runs on a ``CuttingLayout``
    before serialization and on the dict after it.
    """
    return sorted(layouts, key=_is_half)


def _is_half(layout) -> bool:
    material = (
        layout.get("material", {}) if isinstance(layout, dict) else layout.material
    )
    if isinstance(material, dict):
        return bool(material.get("half_board", False))
    return bool(getattr(material, "half_board", False))


def base_label(piece_id: str) -> str:
    """Returns the base label, stripping a single ``#N`` instance suffix."""
    return _INSTANCE_SUFFIX.sub("", piece_id or "")


def layout_signature(layout: dict) -> Tuple:
    """Canonical signature of a layout's cutting pattern.

    Two layouts with the same signature share geometry and base piece labels.
    Ignores the sheet number, the remainders (derived from the pieces) and the
    ``piece_id`` instance suffix.

    ``half_board`` IS part of the signature even though the placements alone
    would already match: a half sheet and a whole one of the same material can
    carry identical pieces (the half's content fits either), and merging them
    would draw one board size for both and hide the half inside the whole
    board's group. ``apply_whole_boards`` promotes every half of a material at
    once, so the signature still survives that pass unchanged.
    """
    material = layout.get("material", {})
    material_key = material.get("material_key")
    half_board = bool(material.get("half_board", False))
    pieces = [
        (
            base_label(str(piece.get("piece_id", ""))),
            piece.get("x"),
            piece.get("y"),
            piece.get("width"),
            piece.get("height"),
            bool(piece.get("rotated", False)),
        )
        for piece in layout.get("placed_pieces", [])
    ]
    pieces.sort(key=lambda p: (p[1], p[2], p[3], p[4], p[0]))
    return (material_key, half_board, tuple(pieces))


def group_layouts(layouts: List[dict]) -> List[dict]:
    """Groups the layouts by cutting pattern, preserving the order of appearance.

    Returns a list of groups; each group keeps the representative layout (the
    first of the pattern) and how many sheets share it::

        {
            "pattern_id": 1,
            "count": 5,
            "sheet_numbers": [1, 2, 3, 4, 5],
            "material_key": "b1",
            "layout": { ...layout representativo... },
        }
    """
    groups: List[dict] = []
    index_by_signature: dict = {}

    for layout in layouts:
        signature = layout_signature(layout)
        sheet_number = layout.get("material", {}).get("sheet_number")

        if signature in index_by_signature:
            group = groups[index_by_signature[signature]]
            group["count"] += 1
            group["sheet_numbers"].append(sheet_number)
            continue

        index_by_signature[signature] = len(groups)
        groups.append(
            {
                "pattern_id": len(groups) + 1,
                "count": 1,
                "sheet_numbers": [sheet_number],
                "material_key": layout.get("material", {}).get("material_key"),
                "layout": layout,
            }
        )

    return groups
