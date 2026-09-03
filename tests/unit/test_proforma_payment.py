"""Unit tests for the "FORMA DE PAGO" block of the commercial documents.

No DB: ``_payment_section`` is a pure static method over the ``ProformaCarrier``
dataclass. The integration suite only asserts that the PDFs render, so this is
where the rows themselves are pinned.
"""

from reportlab.lib.styles import getSampleStyleSheet

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.proforma import ProformaService

_HEADING = getSampleStyleSheet()["Heading2"]


def _carrier(**amounts) -> ProformaCarrier:
    return ProformaCarrier(reference="ORD-2026-0007", client=None, **amounts)


def _rows(**amounts):
    """The rows the block would print for the given registered amounts."""
    block = ProformaService._payment_section(_carrier(**amounts), _HEADING)
    if not block:
        return []
    # The table is the last flowable; `_section` puts the heading before it.
    return block[-1]._cellvalues


def test_no_payment_omits_the_block():
    """Quotes and orders not yet queued carry no payment: nothing is printed."""
    assert ProformaService._payment_section(_carrier(), _HEADING) == []
    assert _rows(payment_cash_amount=0, payment_credit_amount=0) == []


def test_every_method_gets_its_row_and_the_total_adds_up():
    assert _rows(
        payment_cash_amount=10.0,
        payment_transfer_amount=20.0,
        payment_credit_amount=5.0,
    ) == [
        ["Efectivo:", "$10.00"],
        ["Transferencia:", "$20.00"],
        ["A crédito:", "$5.00"],
        ["Total:", "$35.00"],
    ]


def test_only_the_methods_used_are_printed():
    assert _rows(payment_transfer_amount=120.0) == [
        ["Transferencia:", "$120.00"],
        ["Total:", "$120.00"],
    ]


def test_order_registered_before_transfer_existed_renders_unchanged():
    """A pre-feature order leaves the column NULL; the block must not grow a
    ``$0.00`` transfer row for it."""
    assert _rows(payment_cash_amount=50.0, payment_transfer_amount=None) == [
        ["Efectivo:", "$50.00"],
        ["Total:", "$50.00"],
    ]
