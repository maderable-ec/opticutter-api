"""Unit: the stock rules that are pure (no DB, no MySQL).

The two comparisons a stock alert makes are trivial arithmetic; what is NOT
trivial — and what this pins — is the three-way reading of an ABSENT quantity,
and the fact that a threshold is per product type because the units are.

The service's queries (branch, products, settings) are exercised end to end in
``tests/test_inventory.py`` against real rows, per the repo's split.
"""

import pytest

from src.modules.inventory.service import _UNITS, StockService
from src.modules.products.model import ProductType
from src.modules.settings.service import _STOCK_FIELD_MAP

_STOCK = {
    "TABLEROS:155": {1: 558.0, 2: 10.0},
    "TABLEROS:2085": {1: 0.0},  # the vendor knows it; Macas never stocked it
}


def _available(code, warehouse):
    return StockService._available(_STOCK, code, warehouse)


def test_a_stocked_article_reports_its_quantity():
    assert _available("TABLEROS:155", 1) == 558.0
    assert _available("TABLEROS:155", 2) == 10.0


def test_an_article_with_no_row_in_that_warehouse_is_zero():
    """68 of Macas' articles are in exactly this state today: the vendor knows
    the article, it has simply never been stocked there. Zero is the honest
    answer, and it is what the report exists to show."""
    assert _available("TABLEROS:2085", 2) == 0.0


def test_an_article_the_vendor_never_mentions_is_zero_too():
    assert _available("TABLEROS:9999", 1) == 0.0


def test_a_product_the_vendor_does_not_know_is_UNKNOWN_not_zero():
    """A product created by hand never got an ``external_code``, so the vendor
    has no opinion about it. Calling that zero would park a permanent false
    alarm in the report for something nobody can restock through SIFAC."""
    assert _available(None, 1) is None
    assert _available("", 1) is None


@pytest.mark.parametrize(
    "product_type,unit",
    [(ProductType.BOARD.value, "sheets"), (ProductType.EDGE_BANDING.value, "linear_m")],
)
def test_each_product_type_reports_its_own_unit(product_type, unit):
    """The seller reads "3 láminas" or "40 m"; the number alone means neither."""
    assert _UNITS[product_type] == unit


def test_every_product_type_has_a_threshold_and_a_unit():
    """A type with neither is invisible to both the alert and the report, which
    is a silent hole rather than a decision — so make adding one break here."""
    types = {t.value for t in ProductType}
    assert set(_STOCK_FIELD_MAP) == types
    assert set(_UNITS) == types
