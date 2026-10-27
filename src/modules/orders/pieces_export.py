"""An order's cut list in the files of the workshop's commercial cutting program.

The inverse of the web's piece import (``piecesCsv.ts``): the same seven CSV
columns it reads and writes, and the XML that program saves, copied tag for tag
from its own files (a flat ``<data><parts><row>`` list). Loading an order back
into that program is the whole point, so the shape follows ITS files, not ours.

The banding travels in the label, never in ``<edge_band>``: the program's files
always leave that block empty, and an optimizer may shave the tape's thickness
off the cut size, which would change every banded piece.

The sizes are the CUT ones -- the program cuts what the file says: a side with
a hard tape is cut short (``hard_edges.cut_size``), read off the tapes the
order froze, and a layer of an assembly is cut large and alike its other
layers (``assembly``), by the millimetres the order's plan was cut with
(``cut_rules``, read off its snapshot by the caller, never the settings of the
day). ``<edge_band>`` staying empty is what keeps the program
from taking it off again. The flip side: the web's import reads a size as
FINAL, so re-importing this CSV into the wizard would apply both rules twice.

Pure: it reads the order's frozen pieces and touches no session.
"""

import csv
import io
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional, Tuple

from src.modules.optimizations.assembly import (
    Layer,
    assembly_cut_sizes,
    assembly_key,
)
from src.modules.optimizations.cut_rules import CutRules
from src.modules.optimizations.hard_edges import cut_size, frozen_band_types
from src.modules.optimizations.labels import edge_notation, workshop_codes_line
from src.modules.optimizations.schemas import WORKSHOP_CODE_FIELDS
from src.modules.orders.model import OrderPieceModel

# The web import's positional layout (``CSV_COLUMNS``). Prioridad is a slot the
# editor no longer has, kept empty so the file re-imports without shifting.
CSV_COLUMNS = (
    "Material",
    "Largo",
    "Ancho",
    "Cantidad",
    "Prioridad",
    "Etiqueta",
    "Rotar",
)

_SIDES = ("top", "left", "bottom", "right")


def piece_etiqueta(piece: OrderPieceModel) -> str:
    """The piece's label for the other program: banding, own text, then codes.

    ``1L1C CS BLN Puerta Abis X1 Div D1``. The banding comes first because that
    is where the shop writes it and where the web import reads it back from.
    Everything is joined by spaces -- the shop's own ``1L CD 1C CS`` -- so the
    label stays ASCII; the program's files carry nothing else.
    """
    edges = piece.edges or {}
    banding = edge_notation(
        edges.get("sides") or [],
        edges.get("band_type"),
        edges.get("alias"),
        edges.get("special_edges") or (),
        sep=" ",
    )
    codes = workshop_codes_line(
        {f: getattr(piece, f) for f in WORKSHOP_CODE_FIELDS}, sep=" "
    )
    return " ".join(p for p in (banding, (piece.label or "").strip(), codes) if p)


def customer_name(client) -> str:
    """The client as the web's ``clientName`` shows it: the name, else the id."""
    name = f"{client.first_name or ''} {client.last_name or ''}".strip()
    return name or client.identifier or ""


def _material(piece: OrderPieceModel) -> str:
    return piece.product_name or piece.product_code or ""


def _own_cut_size(piece: OrderPieceModel, hard_mm: int) -> Tuple[int, int]:
    """``(largo, ancho)`` the saw cuts for the piece alone: the ordered size
    minus its hard tapes, unless the seller turned the rule off for it."""
    if piece.hard_edge_cut is False:
        return piece.height, piece.width
    return cut_size(piece.height, piece.width, frozen_band_types(piece.edges), hard_mm)


def _layer(piece: OrderPieceModel, hard_mm: int) -> Layer:
    """A frozen piece as the ensamble rule sees it (``assembly.Layer``)."""
    return Layer(
        key=assembly_key(piece.assembly_code) if piece.assembly_allowance else None,
        final=(piece.height, piece.width),
        cut=_own_cut_size(piece, hard_mm),
        quantity=piece.quantity,
        banded=bool(frozen_band_types(piece.edges)),
        code=piece.assembly_code,
    )


def _with_cut_sizes(
    pieces: Iterable[OrderPieceModel], rules: CutRules
) -> List[Tuple[OrderPieceModel, Tuple[int, int]]]:
    """The cut list in order, each piece with the ``(largo, ancho)`` the saw
    cuts -- worked out over the whole list at once, since a layer of an
    assembly is cut alike its other layers."""
    ordered = _ordered(pieces)
    layers = [_layer(p, rules.hard_edge_cut_mm) for p in ordered]
    return list(zip(ordered, assembly_cut_sizes(layers, rules.assembly_allowance_mm)))


def _ordered(pieces: Iterable[OrderPieceModel]) -> List[OrderPieceModel]:
    # ``order.pieces`` has no ORDER BY; the id is the cut list's own order.
    return sorted(pieces, key=lambda p: p.id or 0)


def pieces_csv(pieces: Iterable[OrderPieceModel], rules: CutRules) -> bytes:
    """The cut list as the web's CSV export writes it, BOM included for Excel.

    ``rules``: the millimetres the order's plan was cut with
    (``cut_rules.payload_cut_rules`` of its snapshot)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)  # QUOTE_MINIMAL, CRLF
    writer.writerow(CSV_COLUMNS)
    for piece, (height, width) in _with_cut_sizes(pieces, rules):
        writer.writerow(
            (
                _material(piece),
                height,
                width,
                piece.quantity,
                "",
                piece_etiqueta(piece),
                "sí" if piece.can_rotate else "no",
            )
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _child(parent: ET.Element, tag: str, text: str = "") -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def pieces_xml(
    pieces: Iterable[OrderPieceModel],
    rules: CutRules,
    customer: Optional[str] = None,
) -> bytes:
    """The cut list as the commercial program saves it.

    Same prolog, same tags in the same order, tab indentation and
    ``<tag></tag>`` for an empty one -- never ``<tag />``, which that program
    does not write. ``length`` is the height: the first measurement, along the
    grain. ``customer``, empty in the shop's own files, carries the client.
    ``rules`` as in ``pieces_csv``.
    """
    root = ET.Element("data")
    parts = ET.SubElement(root, "parts")
    for piece, (height, width) in _with_cut_sizes(pieces, rules):
        row = ET.SubElement(parts, "row")
        _child(row, "length", str(height))
        _child(row, "width", str(width))
        _child(row, "quantity", str(piece.quantity))
        _child(row, "grain", "0")
        _child(row, "allow_rotation", "1" if piece.can_rotate else "0")
        _child(row, "label", piece_etiqueta(piece))
        _child(row, "material", _material(piece))
        _child(row, "customer", customer or "")
        edge_band = ET.SubElement(row, "edge_band")
        for side in _SIDES:
            _child(edge_band, f"{side}_name")
            _child(edge_band, f"{side}_thick", "0")
        grinding = ET.SubElement(row, "grinding")
        for side in _SIDES:
            _child(grinding, f"{side}_thick", "0")
        _child(row, "useit", "1")
    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return f'<?xml version="1.0"?>\n{body}\n'.encode("utf-8")
