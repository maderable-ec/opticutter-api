"""Unit tests for the page orientation of the rendered PDFs.

The cut diagram is drawn with the board rotated 90 degrees, so it prints on
landscape sheets (one pattern per sheet) while the piece/board lists stay
portrait. No DB: the render layer is DB-free and ``ProformaCarrier`` is a plain
dataclass, so a synthetic carrier is enough.
"""

from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.proforma import ProformaService

_CLIENT = SimpleNamespace(
    first_name="Ana",
    last_name="Pérez",
    email="ana@example.com",
    phone="0999999999",
    identification="1700000000",
    address="Cuenca",
)


def _layout(width, height, index):
    """One board with a couple of placed pieces, uniquely signed.

    ``group_layouts`` dedupes by ``material_key`` + piece geometry/ids, so each
    layout needs its own key to count as a distinct pattern (= its own page).
    """
    return {
        "material": {
            "width": width,
            "height": height,
            "name": f"Tablero {width}x{height}",
            "code": f"T{index}",
            "material_key": f"b{index}",
            "sheet_number": index,
        },
        "placed_pieces": [
            {
                "piece_id": f"P{index}#1",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 600,
                "label": f"P{index}",
                "edges": {"sides": ["top"], "band_type": "Soft", "family": "CSH"},
            },
            {
                "piece_id": f"Q{index}#1",
                "x": 410,
                "y": 0,
                "width": 300,
                "height": 500,
                "label": f"Q{index}",
                "edges": {},
            },
        ],
        "remainders": [],
        "cuts": [],
        "efficiency": 0.55,
        "pattern_id": index,
    }


def _carrier(layouts):
    return ProformaCarrier(
        reference="ORD-2026-0007",
        client=_CLIENT,
        company={"name": "Maderable"},
        notes="Proyecto Casa Pérez",
        total_boards_used=len(layouts),
        total_boards_cost=120.0,
        requirements=[
            {
                "label": "P1",
                "width": 400,
                "height": 600,
                "quantity": 1,
                "material_name": "Tablero",
                "edge_banding": {"sides": ["top"]},
            }
        ],
        materials_summary=[
            {
                "name": "Tablero",
                "code": "T1",
                "boards_used": len(layouts),
                "unit_price": 40.0,
                "total": 120.0,
                "quantity": len(layouts),
            }
        ],
        layouts=layouts,
    )


def _orientations(buffer):
    """``"L"``/``"P"`` per page, read from each page's own mediabox."""
    return [
        "L" if float(page.mediabox.width) > float(page.mediabox.height) else "P"
        for page in PdfReader(buffer).pages
    ]


def _diagram_sizes(buffer):
    """Drawn width x height (pt) of each embedded diagram, page by page."""
    import re

    pattern = re.compile(r"([\d.]+) 0 0 ([\d.]+) [\d.-]+ [\d.-]+ cm\s*/FormXob")
    sizes = []
    for page in PdfReader(buffer).pages:
        data = page.get_contents().get_data().decode("latin-1")
        sizes.extend((float(w), float(h)) for w, h in pattern.findall(data))
    return sizes


def test_production_sheet_prints_lists_portrait_and_diagrams_landscape():
    carrier = _carrier([_layout(1220, 2440, i) for i in (1, 2, 3)])

    orientations = _orientations(ProformaService.generate_production_sheet_pdf(carrier))

    # The lists come first (portrait) and every pattern gets its own landscape sheet.
    assert orientations[0] == "P"
    assert orientations[1:] == ["L", "L", "L"]


def test_diagram_document_is_landscape_throughout():
    carrier = _carrier([_layout(1220, 2440, i) for i in (1, 2)])

    # Its only content is diagrams, so it is landscape from page 1.
    assert _orientations(ProformaService.generate_diagram_pdf(carrier)) == ["L", "L"]


def test_diagram_pages_carry_no_heading():
    """Nothing shares a diagram sheet: no document header, no section title, so
    every pattern is drawn at the same maximum size. The packet's ORDEN DE PEDIDO
    is what identifies the job."""
    carrier = _carrier([_layout(1220, 2440, i) for i in (1, 2)])

    reader = PdfReader(ProformaService.generate_diagram_pdf(carrier))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert "DIAGRAMA DE DESPIECE" not in text
    assert "DISPOSICIÓN DE CORTES" not in text
    assert "ORD-2026-0007" not in text
    # Only the footer survives.
    assert "Página 1" in text

    sizes = _diagram_sizes(ProformaService.generate_diagram_pdf(carrier))
    assert len(sizes) == 2
    assert sizes[0] == sizes[1]  # uniform: the first one is not shrunk


def test_order_document_mixes_portrait_lists_with_landscape_diagrams():
    carrier = _carrier([_layout(1220, 2440, 1)])

    buffer = ProformaService.generate_proforma_pdf(carrier, title="ORDEN DE PEDIDO")
    reader = PdfReader(buffer)
    orientations = [
        "L" if float(p.mediabox.width) > float(p.mediabox.height) else "P"
        for p in reader.pages
    ]

    assert orientations == ["P", "L"]
    # The commercial pages keep their titles; the diagram sheet is bare.
    assert "ORDEN DE PEDIDO" in (reader.pages[0].extract_text() or "")
    assert "DISPOSICIÓN DE CORTES" not in (reader.pages[1].extract_text() or "")


@pytest.mark.parametrize(
    "render",
    [
        lambda c: ProformaService.generate_proforma_pdf(c, include_diagram=False),
        ProformaService.generate_dispatch_sheet_pdf,
    ],
    ids=["proforma-sin-diagrama", "hoja-de-despacho"],
)
def test_documents_without_a_diagram_stay_portrait(render):
    carrier = _carrier([_layout(1220, 2440, 1)])

    assert set(_orientations(render(carrier))) == {"P"}


def test_diagram_fills_the_landscape_sheet():
    """The whole point of the landscape page: the standard board is drawn at the
    full content width (770pt) instead of the portrait 523pt."""
    carrier = _carrier([_layout(1220, 2440, 1), _layout(1220, 2440, 2)])

    sizes = _diagram_sizes(ProformaService.generate_production_sheet_pdf(carrier))

    assert len(sizes) == 2
    for width, height in sizes:
        assert width == pytest.approx(769.9, abs=1.0)
        assert height == pytest.approx(461.0, abs=1.0)


def test_a_tall_board_is_clamped_to_the_landscape_frame():
    """A landscape frame is *shorter* than a portrait one, so a board whose
    diagram is proportionally tall must be scaled down instead of overflowing
    (which reportlab would reject with a LayoutError)."""
    carrier = _carrier([_layout(2150, 2800, 1)])

    orientations = _orientations(ProformaService.generate_production_sheet_pdf(carrier))
    sizes = _diagram_sizes(ProformaService.generate_production_sheet_pdf(carrier))

    assert orientations == ["P", "L"]
    assert len(sizes) == 1
    width, height = sizes[0]
    assert height <= 522.1  # the landscape frame's usable height
    assert width < 769.9  # scaled down proportionally, not stretched
