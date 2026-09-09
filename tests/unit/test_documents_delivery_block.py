"""Unit tests for the delivery block the ORDEN DE PEDIDO absorbed.

The dispatch sheet is gone: it repeated the piece list, the payment box and the
whole letterhead only to carry three things — the delivery line, the disclaimer
and the signatures — which now live on the order's one document.

No DB: the carrier is a plain dataclass and the builders are static methods.
"""

from datetime import datetime

from pypdf import PdfReader
from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import DocumentCarrier
from src.modules.optimizations.documents import DISPATCH_DISCLAIMER, DocumentService

_STYLES = getSampleStyleSheet()


class _Client:
    first_name = "Juan"
    last_name = "Pérez"
    phone = "0999999999"
    email = None


def _carrier(**kwargs) -> DocumentCarrier:
    return DocumentCarrier(reference="ORD-2026-0007", client=_Client(), **kwargs)


def _dispatch_text(**kwargs) -> str:
    return DocumentService._dispatch_line(_carrier(**kwargs), _STYLES).text


def _document_text(carrier: DocumentCarrier) -> str:
    reader = PdfReader(DocumentService.generate_order_document_pdf(carrier))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_an_undispatched_order_prints_rules_to_fill_in_by_hand():
    """The shop prints this the moment the cutting is done, which is *before*
    anyone delivers anything. It used to stamp ``datetime.now()`` as the dispatch
    date — a made-up fact on a document the client signs."""
    text = _dispatch_text()

    assert "____" in text
    assert datetime.now().strftime("%d/%m/%Y") not in text


def test_a_dispatched_order_prints_the_frozen_data():
    text = _dispatch_text(
        dispatch_date=datetime(2026, 9, 8), dispatched_by_label="Denis Siavichay"
    )

    assert "08/09/2026" in text
    assert "Denis Siavichay" in text
    assert "____" not in text


def test_a_half_dispatched_row_only_blanks_the_missing_half():
    """The two fields are independent: one frozen value does not fill the other."""
    text = _dispatch_text(dispatch_date=datetime(2026, 9, 8))

    assert "08/09/2026" in text
    assert "____" in text


def test_the_responsible_name_is_escaped():
    """It reaches a ``Paragraph``, which parses mini-HTML — the same reason
    ``_reference_lines`` escapes the commercial reference."""
    assert "&amp;" in _dispatch_text(dispatched_by_label="Salazar & Hijos")


def test_the_order_document_carries_the_disclaimer_and_the_signatures():
    text = _document_text(_carrier())

    assert "DESCARGO DE RESPONSABILIDAD" in text
    assert DISPATCH_DISCLAIMER[:60] in " ".join(text.split())
    assert "Recibí conforme" in text
    assert text.count("Entregado por") == 3


def test_the_delivery_line_sits_with_the_client_block_not_at_the_end():
    """It is filled in at the counter alongside the client's data; the signatures
    are what close the document."""
    text = _document_text(_carrier())

    assert text.index("Fecha de despacho") < text.index("DESCARGO DE RESPONSABILIDAD")
    assert text.index("DETALLE DE REQUERIMIENTOS") > text.index("Fecha de despacho")
