"""The order's cut list in the commercial cutting program's CSV and XML."""

import csv
import io
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from src.modules.optimizations.labels import edge_notation, workshop_codes_line
from src.modules.orders.pieces_export import (
    CSV_COLUMNS,
    customer_name,
    piece_etiqueta,
    pieces_csv,
    pieces_xml,
)

# The tags of one <row>, in order, as the commercial program writes them (read
# off its own files; they live in ``pruebas/``, which git ignores).
_ROW_TAGS = [
    "length",
    "width",
    "quantity",
    "grain",
    "allow_rotation",
    "label",
    "material",
    "customer",
    "edge_band",
    "grinding",
    "useit",
]
_EDGE_BAND_TAGS = [
    "top_name",
    "top_thick",
    "left_name",
    "left_thick",
    "bottom_name",
    "bottom_thick",
    "right_name",
    "right_thick",
]


def _piece(id=1, **kw):
    fields = dict(
        id=id,
        product_name="Melamina Blanca RH 15",
        product_code="MEL15",
        label="Puerta",
        height=720,
        width=400,
        quantity=2,
        can_rotate=True,
        edges=None,
        hinging_code=None,
        grooving_code=None,
        assembly_code=None,
        division_code=None,
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------- #
# Etiqueta
# --------------------------------------------------------------------------- #
def test_an_unbanded_piece_keeps_only_its_own_text():
    # No "NO": the web import would read "NO Puerta" as the piece's text.
    assert piece_etiqueta(_piece()) == "Puerta"
    assert piece_etiqueta(_piece(label=None)) == ""


def test_the_banding_leads_with_its_type_and_alias():
    edges = {"sides": ["left", "top"], "band_type": "Soft", "alias": "bln"}
    assert piece_etiqueta(_piece(edges=edges)) == "1L1C CS BLN Puerta"


def test_special_edges_join_with_a_space_not_a_middle_dot():
    edges = {
        "sides": ["left"],
        "band_type": "Hard",
        "alias": "BLN",
        "special_edges": [{"side": "top", "band_type": "Soft", "alias": "CHM"}],
    }
    assert piece_etiqueta(_piece(label="", edges=edges)) == "1L CD BLN 1C CS CHM"


def test_workshop_codes_close_the_label():
    piece = _piece(
        edges={"sides": ["left"], "band_type": "Soft", "alias": None},
        hinging_code="X1",
        division_code="1/60",
    )
    assert piece_etiqueta(piece) == "1L CS Puerta Abis X1 Div 1/60"


def test_the_printed_notation_keeps_its_middle_dot():
    special = [{"side": "top", "band_type": "Soft", "alias": "CHM"}]
    assert edge_notation(["left"], "Hard", "BLN", special) == "1L CD BLN · 1C CS CHM"
    assert (
        workshop_codes_line({"hinging_code": "X1", "division_code": "D1"})
        == "Abis X1 · Div D1"
    )


def test_customer_name_falls_back_to_the_identifier():
    ada = SimpleNamespace(
        first_name="Ada", last_name="Lovelace", identifier="0100000397"
    )
    nameless = SimpleNamespace(first_name=None, last_name=" ", identifier="0100000397")
    assert customer_name(ada) == "Ada Lovelace"
    assert customer_name(nameless) == "0100000397"


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #
def test_the_xml_has_the_commercial_programs_shape():
    raw = pieces_xml([_piece()], "Ada Lovelace").decode("utf-8")
    assert raw.startswith('<?xml version="1.0"?>\n<data>\n\t<parts>\n\t\t<row>\n')
    assert raw.endswith("\t</parts>\n</data>\n")
    # An empty tag is written open and closed, never self-closed.
    assert "<top_name></top_name>" in raw
    assert "/>" not in raw

    row = ET.fromstring(raw).find("parts/row")
    assert [child.tag for child in row] == _ROW_TAGS
    assert [child.tag for child in row.find("edge_band")] == _EDGE_BAND_TAGS
    assert all(
        (child.text or "") in ("", "0") for child in row.find("edge_band")
    ), "the banding travels in the label, never in <edge_band>"
    assert [child.text for child in row.find("grinding")] == ["0"] * 4


def test_the_xml_reads_back_as_the_benchmarks_read_the_shops_files():
    pieces = [
        _piece(id=2, height=300, width=580, quantity=1, can_rotate=False, label="Base"),
        _piece(
            id=1,
            edges={"sides": ["left", "right"], "band_type": "Hard", "alias": None},
        ),
    ]
    root = ET.fromstring(pieces_xml(pieces, "Ada Lovelace"))
    rows = [{child.tag: child.text for child in row} for row in root.find("parts")]
    # Same reading as scripts/bench_shopfiles.py, in cut-list (id) order.
    assert [
        (
            r["material"],
            int(r["length"]),
            int(r["width"]),
            int(r["quantity"]),
            r["allow_rotation"] == "1",
            r["label"],
            r["useit"],
            r["customer"],
        )
        for r in rows
    ] == [
        (
            "Melamina Blanca RH 15",
            720,
            400,
            2,
            True,
            "2L CD Puerta",
            "1",
            "Ada Lovelace",
        ),
        ("Melamina Blanca RH 15", 300, 580, 1, False, "Base", "1", "Ada Lovelace"),
    ]


def test_an_inline_material_falls_back_to_its_code():
    row = ET.fromstring(pieces_xml([_piece(product_name=None)])).find("parts/row")
    assert row.find("material").text == "MEL15"
    assert row.find("customer").text is None  # empty, as in the shop's own files


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_the_csv_is_the_web_exports_layout():
    pieces = [
        _piece(id=2, label="Tapa, frontal", can_rotate=False),
        _piece(id=1, edges={"sides": ["top"], "band_type": "Soft", "alias": None}),
    ]
    raw = pieces_csv(pieces).decode("utf-8")
    assert raw.startswith("﻿"), "BOM, so Excel opens it as UTF-8"
    assert "\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.lstrip("﻿"))))
    assert rows == [
        list(CSV_COLUMNS),
        ["Melamina Blanca RH 15", "720", "400", "2", "", "1C CS Puerta", "sí"],
        ["Melamina Blanca RH 15", "720", "400", "2", "", "Tapa, frontal", "no"],
    ]
