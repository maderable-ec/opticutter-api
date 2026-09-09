"""Unit tests for the "sin refilar" mark on the documents.

No DB: the three material tables are pure functions over the ``DocumentCarrier``.
The mark exists because the shop squares a board by reflex — a plan that
deliberately uses the full sheet has to say so on the paper the operator holds,
not only in the quote that priced it.

It is a suffix and not a column on purpose: none of the three tables has room
for a sixth column, and the summary already appends "(medio tablero)" the same
way.
"""

from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import DocumentCarrier
from src.modules.optimizations.documents import DocumentService, _material_label

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
        "skip_trim": False,
    }
    entry.update(over)
    return entry


def _carrier(*materials, **over) -> DocumentCarrier:
    return DocumentCarrier(
        reference="ORD-2026-0007",
        client=None,
        materials_summary=list(materials),
        **over,
    )


def test_a_trimmed_material_prints_exactly_as_before():
    assert _material_label(_material("b1", "catalog"), "N/A") == "Melamina Blanca"


def test_an_untrimmed_material_says_so_next_to_its_name():
    label = _material_label(_material("b1", "catalog", skip_trim=True), "N/A")
    assert label.startswith("Melamina Blanca")
    assert "sin refilar" in label


def test_the_mark_survives_a_payload_that_predates_the_field():
    """A snapshot frozen before the flag existed has no key at all.

    Orders keep their optimization forever, so every reader defaults it rather
    than assuming the shape of today's payload.
    """
    old = _material("b1", "catalog")
    del old["skip_trim"]
    assert _material_label(old, "N/A") == "Melamina Blanca"


def test_the_mark_falls_back_with_the_name_it_annotates():
    """Fallback first, mark second: an unnamed material still gets flagged."""
    entry = _material("b1", "catalog", product_name=None, skip_trim=True)
    assert _material_label(entry, "MEL18").startswith("MEL18")
    assert "sin refilar" in _material_label(entry, "MEL18")


def test_the_priced_table_carries_the_mark():
    """The ORDEN DE PEDIDO: the commercial record of what was agreed."""
    carrier = _carrier(_material("b1", "catalog", skip_trim=True))
    rows = DocumentService._build_materials_table(carrier, _CELL)._cellvalues
    assert "sin refilar" in rows[1][1].text
    # The mark never leaks into the money columns.
    assert rows[1][3] == "$45.50"


def test_the_client_material_table_carries_the_mark():
    """Where it matters most: a retazo the client brought already squared."""
    entry = _material(
        "r1", "clientOffcut", product_name="Retazo grande", skip_trim=True
    )
    rows = DocumentService._build_client_material_table([entry], _CELL)._cellvalues
    assert "sin refilar" in rows[1][0].text


def test_a_trimmed_job_prints_no_mark_anywhere():
    carrier = _carrier(_material("b1", "catalog"), _material("r1", "clientOffcut"))
    priced = DocumentService._build_materials_table(carrier, _CELL)._cellvalues
    client = DocumentService._build_client_material_table(
        [_material("r1", "clientOffcut")], _CELL
    )._cellvalues
    printed = [str(getattr(c, "text", c)) for row in priced + client for c in row]
    assert not any("sin refilar" in cell for cell in printed)
