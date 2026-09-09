"""Unit tests for the packet: one body, numbered once, naming its boards.

An order emits a single PDF — ORDEN DE PEDIDO, then the cut diagrams, then the
annexes — that is printed and handed over as one document. Two things follow
from that and are pinned here: the page numbering runs end to end (each document
used to number its own, so a printout said "Página 1" twice), and the diagram
header names the board it is cut on instead of a pattern index.

No DB: the render layer is DB-free and ``DocumentCarrier`` is a plain dataclass.
"""

from types import SimpleNamespace

from pypdf import PdfReader

from src.modules.optimizations.carrier import DocumentCarrier
from src.modules.optimizations.documents import build_order_packet

_CLIENT = SimpleNamespace(
    first_name="Ana", last_name="Pérez", email="ana@example.com", phone="0999999999"
)


def _layout(key, half=False, sheet=1):
    width = 1035 if half else 2070
    return {
        "material": {
            "material_key": key,
            "sheet_number": sheet,
            "width": width,
            "height": 2440,
            "thickness": 15,
            "area": width * 2440,
            "cost_per_unit": 42.5,
            "half_board": half,
        },
        "placed_pieces": [
            {
                "piece_id": f"{key}{sheet}#1",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 600,
                "label": "Puerta",
                "edges": {"sides": ["top"], "band_type": "Soft"},
            }
        ],
        "statistics": {"used_area": 240000, "efficiency": 9.5},
        "remainders": [],
        "cuts": [],
    }


def _summary(key, name, half=False):
    return {
        "material_key": key,
        "product_code": key.upper(),
        "product_name": name,
        "half_board": half,
        "count": 1,
        "source": "catalog",
    }


def _carrier(layouts, summaries):
    return DocumentCarrier(
        reference="ORD-2026-0184",
        client=_CLIENT,
        company={"name": "Maderable"},
        layouts=layouts,
        materials_summary=summaries,
        requirements=[{"label": "Puerta", "width": 400, "height": 600, "quantity": 1}],
        subtotal=100.0,
        tax_rate=0.15,
        tax_amount=15.0,
        total_cost=115.0,
    )


def _footers(pdf):
    """The stamped footer line of every page, in order."""
    out = []
    for page in PdfReader(pdf).pages:
        for line in (page.extract_text() or "").splitlines():
            if "Página" in line:
                out.append(line.strip())
    return out


def _annex(pages: int = 1) -> bytes:
    """A real, minimal PDF annex of ``pages`` pages."""
    import io

    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    pdf = rl_canvas.Canvas(buf)
    for _ in range(pages):
        pdf.drawString(100, 700, "anexo")
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def test_the_packet_is_numbered_once_end_to_end():
    """The order, the diagrams and the annexes are one body, not three that each
    restart at page 1 — which is what the printed copy showed."""
    carrier = _carrier(
        [_layout("b1"), _layout("b2", sheet=2)],
        [_summary("b1", "Melamina Blanco 15mm"), _summary("b2", "Melamina Cashmere")],
    )

    footers = _footers(build_order_packet(carrier, [(_annex(), "application/pdf")]))

    assert footers == [
        f"Página {n} de {len(footers)}" for n in range(1, len(footers) + 1)
    ]
    # order (1) + one page per pattern (2) + the annex (1), at least.
    assert len(footers) >= 4


def test_every_page_carries_the_order_number():
    """A sheet separated from the rest on the shop floor still says which order
    it belongs to."""
    carrier = _carrier([_layout("b1")], [_summary("b1", "Melamina Blanco 15mm")])

    reader = PdfReader(build_order_packet(carrier, []))

    assert reader.pages
    for page in reader.pages:
        assert "N° ORD-2026-0184" in (page.extract_text() or "")


def test_the_annex_pages_are_numbered_too():
    carrier = _carrier([_layout("b1")], [_summary("b1", "Melamina Blanco 15mm")])

    without = len(_footers(build_order_packet(carrier, [])))
    with_annex = _footers(build_order_packet(carrier, [(_annex(2), "application/pdf")]))

    assert len(with_annex) == without + 2
    assert with_annex[-1] == f"Página {without + 2} de {without + 2}"


# --- The diagram header names the board ---------------------------------------
def test_the_diagram_header_gets_the_boards_name_not_a_pattern_index():
    """ "Tablero 1" is a pattern index, which means nothing at the saw: on a
    multi-material job the operator has to know WHICH board this one is cut on."""
    from src.modules.optimizations import documents

    carrier = _carrier(
        [_layout("b1"), _layout("b2", sheet=2)],
        [_summary("b1", "Melamina Blanco 15mm"), _summary("b2", "Melamina Cashmere")],
    )

    assert documents._board_names(carrier) == {
        ("b1", False): "Melamina Blanco 15mm",
        ("b2", False): "Melamina Cashmere",
    }


def test_a_half_board_keeps_its_own_name():
    """Keyed on ``(material_key, half_board)``: the same board cut both ways is
    two summary lines, and only one of them says "(medio tablero)"."""
    from src.modules.optimizations import documents

    carrier = _carrier(
        [_layout("b1"), _layout("b1", half=True, sheet=2)],
        [
            _summary("b1", "Melamina Cashmere 18mm"),
            _summary("b1", "Melamina Cashmere 18mm (medio tablero)", half=True),
        ],
    )

    names = documents._board_names(carrier)
    assert names[("b1", False)] == "Melamina Cashmere 18mm"
    assert names[("b1", True)] == "Melamina Cashmere 18mm (medio tablero)"


def test_an_untrimmed_board_says_so_on_the_diagram_too():
    """The operator holding the diagram is the one who would square a board the
    plan used edge to edge — the same reason the material tables carry the mark."""
    from src.modules.optimizations import documents

    carrier = _carrier(
        [_layout("b1")],
        [{**_summary("b1", "Melamina Blanco 15mm"), "skip_trim": True}],
    )

    assert documents._board_names(carrier)[("b1", False)] == (
        "Melamina Blanco 15mm · sin refilar"
    )


def test_the_name_reaches_the_drawing(monkeypatch):
    """The pair that resolves the name is read off the layout's own material, so
    the half board on page 2 cannot inherit the full board's header."""
    from src.modules.optimizations import documents

    seen = []

    def _spy(group, mono=False, board_name=None, **kwargs):
        seen.append(board_name)
        return real(group, mono=mono, board_name=board_name, **kwargs)

    real = documents.VisualizationService.generate_layout_image
    monkeypatch.setattr(documents.VisualizationService, "generate_layout_image", _spy)

    carrier = _carrier(
        [_layout("b1"), _layout("b1", half=True, sheet=2)],
        [
            _summary("b1", "Melamina Cashmere 18mm"),
            _summary("b1", "Melamina Cashmere 18mm (medio tablero)", half=True),
        ],
    )
    build_order_packet(carrier, [])

    assert seen == [
        "Melamina Cashmere 18mm",
        "Melamina Cashmere 18mm (medio tablero)",
    ]
