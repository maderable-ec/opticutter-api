"""An order's cut list in the files of the workshop's commercial cutting program.

The inverse of the web's piece import (``piecesCsv.ts``): the same seven CSV
columns it reads and writes, and the XML that program saves, copied tag for tag
from its own files (a flat ``<data><parts><row>`` list). Loading an order back
into that program is the whole point, so the shape follows ITS files, not ours.

The banding travels in the label, never in ``<edge_band>``: the program's files
always leave that block empty, and an optimizer may shave the tape's thickness
off the cut size, which would change every banded piece.

The sizes are the CUT ones -- the program cuts what the file says: a side with
a hard tape takes 1 mm off (``hard_edges.cut_size``), read off the tapes the
order froze. ``<edge_band>`` staying empty is what keeps the program from
taking it off again. The flip side: the web's import reads a size as FINAL, so
re-importing this CSV into the wizard would take the millimetre off twice.

Pure: it reads the order's frozen pieces and touches no session.
"""

import csv
import io
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional, Tuple

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


def _cut_size(piece: OrderPieceModel) -> Tuple[int, int]:
    """``(largo, ancho)`` the saw cuts: the ordered size minus its hard tapes,
    unless the seller turned the rule off for the piece."""
    if piece.hard_edge_cut is False:
        return piece.height, piece.width
    return cut_size(piece.height, piece.width, frozen_band_types(piece.edges))


def _ordered(pieces: Iterable[OrderPieceModel]) -> List[OrderPieceModel]:
    # ``order.pieces`` has no ORDER BY; the id is the cut list's own order.
    return sorted(pieces, key=lambda p: p.id or 0)


def pieces_csv(pieces: Iterable[OrderPieceModel]) -> bytes:
    """The cut list as the web's CSV export writes it, BOM included for Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)  # QUOTE_MINIMAL, CRLF
    writer.writerow(CSV_COLUMNS)
    for piece in _ordered(pieces):
        height, width = _cut_size(piece)
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
    pieces: Iterable[OrderPieceModel], customer: Optional[str] = None
) -> bytes:
    """The cut list as the commercial program saves it.

    Same prolog, same tags in the same order, tab indentation and
    ``<tag></tag>`` for an empty one -- never ``<tag />``, which that program
    does not write. ``length`` is the height: the first measurement, along the
    grain. ``customer``, empty in the shop's own files, carries the client.
    """
    root = ET.Element("data")
    parts = ET.SubElement(root, "parts")
    for piece in _ordered(pieces):
        row = ET.SubElement(parts, "row")
        height, width = _cut_size(piece)
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
