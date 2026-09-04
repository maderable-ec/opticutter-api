"""Unit tests for how the client's own retazos are rendered on the documents.

No DB: the two row selectors and the two tables are pure functions over the
``ProformaCarrier`` dataclass. The integration suite only asserts the PDFs
render, so this is where the split between "billed" and "brought by the client"
is pinned.

The rule, decided with the shop: a client's retazo has no price — the cutting
and the banding are charged as additional services — so it is listed for
traceability but never in a priced table, and a totals row that would print
``$0.00`` is omitted rather than printed.
"""

from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.proforma import (
    ProformaService,
    _billable_material_rows,
    _client_material_rows,
)

_CELL = getSampleStyleSheet()["BodyText"]


def _material(key, source, **over):
    entry = {
        "material_key": key,
        "source": source,
        "product_id": 7 if source == "catalog" else None,
        "product_code": "MEL18" if source == "catalog" else key,
        "product_name": "Melamina Blanca" if source == "catalog" else None,
        "height": 2440,
        "width": 1220,
        "thickness": 18,
        "count": 2,
        "cost_per_unit": 45.5 if source == "catalog" else 0.0,
        "total_cost": 91.0 if source == "catalog" else 0.0,
    }
    entry.update(over)
    return entry


def _carrier(*materials, **over) -> ProformaCarrier:
    return ProformaCarrier(
        reference="PRE-2026-0007",
        client=None,
        materials_summary=list(materials),
        **over,
    )


def test_client_retazos_are_kept_out_of_the_priced_table():
    carrier = _carrier(
        _material("b1", "catalog"),
        _material("r1", "clientOffcut", product_name="Retazo grande"),
    )
    assert [m["material_key"] for m in _billable_material_rows(carrier)] == ["b1"]
    assert [m["material_key"] for m in _client_material_rows(carrier)] == ["r1"]


def test_the_workshops_own_retazo_stays_in_the_priced_table():
    """It is the shop's material and it carries a price, so it is billed."""
    carrier = _carrier(_material("r1", "companyOffcut", cost_per_unit=12.5))
    assert [m["material_key"] for m in _billable_material_rows(carrier)] == ["r1"]
    assert _client_material_rows(carrier) == []


def test_client_material_table_has_no_money_column():
    rows = ProformaService._build_client_material_table(
        [_material("r1", "clientOffcut", product_name="Retazo grande")], _CELL
    )._cellvalues
    assert rows[0] == ["Descripción", "Dimensiones", "Espesor", "Hojas"]
    assert rows[1][1:] == ["2440×1220 mm", "18 mm", "2"]
    assert not any("$" in str(cell) for row in rows for cell in row)


def test_client_material_never_prints_the_internal_key():
    """``product_code`` is the material KEY for an inline material.

    ``build_materials_summary`` fills ``product_name`` with the seller's label or,
    failing that, the dimensions — so the real payload always has something
    readable. The guard is for the shape that does not: the client must never be
    handed a document naming ``mat-1``.
    """
    unlabelled = _material("r1", "clientOffcut", product_name=None)
    rows = ProformaService._build_client_material_table([unlabelled], _CELL)._cellvalues
    assert rows[1][0].text == "Material del cliente"

    from_summary = _material("r1", "clientOffcut", product_name="1000×1000")
    rows = ProformaService._build_client_material_table(
        [from_summary], _CELL
    )._cellvalues
    assert rows[1][0].text == "1000×1000"


def test_totals_omit_the_rows_that_would_print_zero():
    """A job cut on the client's material buys no board and may band nothing."""
    carrier = _carrier(
        _material("r1", "clientOffcut"),
        total_boards_used=0,
        total_boards_cost=0.0,
        subtotal=17.39,
        tax_rate=0.15,
        tax_amount=2.61,
        total_cost=20.0,
        additional_services=[{"name": "Corte", "unit_price": 20.0, "quantity": 1}],
        services_total=17.39,
    )
    rows = ProformaService._build_totals_table(carrier)._cellvalues
    labels = [row[0] for row in rows]
    assert "Costo de tableros:" not in labels
    assert "Total de tableros utilizados:" not in labels
    assert labels == [
        "Servicios adicionales:",
        "Subtotal:",
        "IVA (15%):",
        "Costo total estimado:",
    ]


def test_an_offcut_only_quote_with_banding_prints_only_the_banding():
    """The banding is billed; the material it went on is the client's."""
    carrier = _carrier(
        _material("r1", "clientOffcut"),
        total_boards_used=0,
        total_boards_cost=0.0,
        total_edge_banding_cost=4.0,
        edge_bandings_summary=[{"product_code": "TC", "total_cost": 4.0}],
        subtotal=4.0,
        tax_rate=0.15,
        tax_amount=0.6,
        total_cost=4.6,
    )
    labels = [
        row[0] for row in ProformaService._build_totals_table(carrier)._cellvalues
    ]
    assert "Costo de tableros:" not in labels
    assert labels[0] == "Costo de tapacantos:"


def test_a_catalog_quote_still_prints_its_board_cost():
    carrier = _carrier(
        _material("b1", "catalog"),
        total_boards_used=2,
        total_boards_cost=91.0,
        total_edge_banding_cost=4.0,
        edge_bandings_summary=[{"product_code": "TC", "total_cost": 4.0}],
        subtotal=95.0,
        tax_rate=0.15,
        tax_amount=14.25,
        total_cost=109.25,
    )
    labels = [
        row[0] for row in ProformaService._build_totals_table(carrier)._cellvalues
    ]
    assert labels[:2] == ["Costo de tableros:", "Costo de tapacantos:"]


def test_a_quote_with_boards_but_no_banding_keeps_the_board_count():
    """The pre-feature shape: no banding, so the count row is what is printed."""
    carrier = _carrier(
        _material("b1", "catalog", cost_per_unit=0.0, total_cost=0.0),
        total_boards_used=2,
        total_boards_cost=0.0,
        subtotal=0.0,
        tax_rate=0.15,
        tax_amount=0.0,
        total_cost=0.0,
    )
    labels = [
        row[0] for row in ProformaService._build_totals_table(carrier)._cellvalues
    ]
    assert labels[0] == "Total de tableros utilizados:"


def test_the_workshop_sheet_counts_every_sheet_it_has_to_cut():
    """Not ``total_boards_used``: that one answers what the CLIENT buys.

    On a job made only of the client's material it is 0, which read as "nothing to
    cut" right next to a table listing two retazos.
    """
    carrier = _carrier(
        _material("r1", "clientOffcut", count=2),
        _material("r2", "clientOffcut", count=1),
        total_boards_used=0,
    )
    rows = ProformaService._build_boards_total_table(carrier)._cellvalues
    assert rows == [["Total de hojas a cortar:", "3"]]


def test_the_workshop_sheet_counts_the_retazo_next_to_the_board():
    carrier = _carrier(
        _material("b1", "catalog", count=2),
        _material("r1", "clientOffcut", count=1),
        total_boards_used=2,
    )
    rows = ProformaService._build_boards_total_table(carrier)._cellvalues
    assert rows == [["Total de hojas a cortar:", "3"]]
