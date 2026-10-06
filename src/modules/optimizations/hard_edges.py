"""The cut size of a piece: its final size minus what its hard tapes add.

A hard tape (``BandType.HARD``, PVC 1-1.5 mm) makes the piece thicker across
the side it is glued on, so the saw has to cut that side short for the piece
to come out at the size the seller typed. The seller always enters the FINAL
size; this module is the one place that turns it into the size the saw cuts.

The axis is the physical one: a tape on a long side (``left``/``right``, the
``L`` of the notation, sides of length ``height``) adds its thickness across
the WIDTH, and one on a short side (``top``/``bottom``, ``C``) across the
HEIGHT. A 600×400 piece with ``2L CD`` is cut 600×398; with ``1C CD``, 599×400.

What makes a side hard is the tape's PRODUCT (its ``bandType``), the same
source that hatches the side in the diagram and prints ``CD`` -- so what reads
``CD`` is exactly what was taken off. The web mirrors this in
``shared/utils/hardEdges.ts`` for the live hint in the despiece.

Pure: no DB, no framework. Two adapters feed ``cut_size`` its band types: a
live requirement against the resolved tapes, and an order's frozen edges.
"""

from typing import Dict, Mapping, Optional, Tuple

from src.modules.optimizations.schemas import Requirement
from src.modules.products.model import ProductModel
from src.modules.products.types.edge_banding import BandType

# Millimetres the saw cuts short per side with a hard tape. A fixed shop rule,
# not the tape's own thickness: a 1.5 mm tape still takes 1 mm off.
HARD_EDGE_CUT_MM = 1

_HEIGHT_SIDES = ("top", "bottom")  # the tape on these adds across the height
_WIDTH_SIDES = ("left", "right")  # ... and on these across the width


def _shorten(size: int, hard_sides: int) -> int:
    cut = size - hard_sides * HARD_EDGE_CUT_MM
    # A piece too small to take its discount keeps its size: coerced, never
    # raised, because pre-orders re-validate on every read (a 422 there is a
    # 500). No real piece is 2 mm wide; this only keeps ``Piece`` positive.
    return cut if cut > 0 else size


def cut_size(
    height: int, width: int, side_band_types: Mapping[str, Optional[str]]
) -> Tuple[int, int]:
    """``(height, width)`` the saw cuts, from the final size and each side's tape.

    ``side_band_types`` maps a NOMINAL side to its tape's band type (``Hard``,
    ``Soft`` or ``None`` for an unknown tape); only ``Hard`` takes anything off.
    """
    hard = {side for side, bt in side_band_types.items() if bt == BandType.HARD.value}
    return (
        _shorten(height, sum(1 for s in _HEIGHT_SIDES if s in hard)),
        _shorten(width, sum(1 for s in _WIDTH_SIDES if s in hard)),
    )


def requirement_band_types(
    req: Requirement, eb_products: Mapping[int, ProductModel]
) -> Dict[str, Optional[str]]:
    """``{nominal side: band type}`` of a live requirement.

    ``req.side_products()`` already resolves which tape each side gets (a canto
    especial on its own side, the auto tape on the rest); the type is read off
    the resolved product. A side whose tape has no product yet (geometry-only
    banding, the product assigned when quoting) is ``None``: not hard.
    """
    types: Dict[str, Optional[str]] = {}
    for side, pid in req.side_products().items():
        product = eb_products.get(pid) if pid is not None else None
        attrs = (product.attributes if product else None) or {}
        types[side] = attrs.get("bandType")
    return types


def frozen_band_types(edges: Optional[Mapping]) -> Dict[str, Optional[str]]:
    """``{nominal side: band type}`` of an order's frozen ``order_pieces.edges``.

    The auto tape's ``band_type`` covers its ``sides``; each entry of
    ``special_edges`` carries its own and wins its side -- an order frozen while
    a special edge still replaced the auto tape lists that side in both.
    """
    edges = edges or {}
    types: Dict[str, Optional[str]] = {
        side: edges.get("band_type") for side in edges.get("sides") or []
    }
    for special in edges.get("special_edges") or []:
        types[special["side"]] = special.get("band_type")
    return types


def requirement_cut_size(
    req: Requirement, eb_products: Mapping[int, ProductModel]
) -> Tuple[int, int]:
    """``(height, width)`` the saw cuts for a live requirement.

    A piece the seller opted out of the rule (``hard_edge_cut=False``) is cut at
    its final size whatever its tapes are.
    """
    if not req.hard_edge_cut:
        return req.height, req.width
    return cut_size(req.height, req.width, requirement_band_types(req, eb_products))
