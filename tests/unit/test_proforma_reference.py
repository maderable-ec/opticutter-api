"""Unit tests for the commercial reference (``notes``) carried into the PDFs.

No DB: ``ProformaCarrier`` is a plain dataclass and ``_reference_lines`` a pure
helper over it.
"""

from types import SimpleNamespace

from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.proforma import _reference_lines

_META_STYLE = getSampleStyleSheet()["Normal"]


def _fake_order(notes):
    """Minimal duck-typed order for ``ProformaCarrier.from_order``."""
    return SimpleNamespace(
        optimization_snapshot={},
        code="ORD-2026-0007",
        client=None,
        total_boards_used=3,
        subtotal=0.0,
        additional_services_total=0.0,
        tax_rate=0.15,
        tax_amount=0.0,
        total=0.0,
        dispatched_at=None,
        dispatched_by_label=None,
        payment_cash_amount=None,
        payment_transfer_amount=None,
        payment_credit_amount=None,
        notes=notes,
    )


def test_from_payload_carries_notes():
    carrier = ProformaCarrier.from_payload(
        {}, client=None, reference="PRE-2026-0001", notes="Proyecto Casa Pérez"
    )
    assert carrier.notes == "Proyecto Casa Pérez"


def test_from_payload_without_notes_defaults_to_none():
    carrier = ProformaCarrier.from_payload({}, client=None, reference="PRE-2026-0001")
    assert carrier.notes is None


def test_from_order_propagates_the_frozen_reference():
    """The order's ``notes`` reaches every document it renders (it isn't in the
    optimization snapshot, so the carrier has to read the column)."""
    carrier = ProformaCarrier.from_order(_fake_order("Obra Los Álamos — cocina"))
    assert carrier.notes == "Obra Los Álamos — cocina"


def test_reference_lines_omitted_without_a_reference():
    for blank in (None, "", "   "):
        carrier = ProformaCarrier(reference="PRE-1", client=None, notes=blank)
        assert _reference_lines(carrier, _META_STYLE) == []


def test_reference_lines_escapes_user_markup():
    """``Paragraph`` parses mini-HTML: an unescaped ``<`` or ``&`` would blow up
    the render (or swallow the text) instead of printing literally."""
    carrier = ProformaCarrier(
        reference="PRE-1", client=None, notes="Cocina <A&B> muebles"
    )
    (paragraph,) = _reference_lines(carrier, _META_STYLE)
    assert paragraph.text == "Ref: Cocina &lt;A&amp;B&gt; muebles"
