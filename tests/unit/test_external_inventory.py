"""Unit: the external stock reader (no MySQL, no DB).

Covers the ``binventario`` row -> ``StockRow`` mapping, the ``cin``-keyed
``external_code`` the rest of the system joins on, the retirement filter that is
what makes that key unique, and the deliberate refusal to degrade quietly when
the source is unreachable.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from src.modules.inventory.external_inventory import ExternalInventorySource
from src.shared.exceptions import ExternalServiceError


def _record(**overrides):
    """One joined ``binventario``/``marticulo`` row with the vendor's types."""
    base = {
        "categoria": "TABLEROS",
        "codigo": "155",
        "est": 1,
        "fec_eli": None,
        "bodega": 1,
        "cantidad": Decimal("558.000"),
    }
    base.update(overrides)
    return base


class _FakeResult:
    def __init__(self, records):
        self._records = records

    def mappings(self):
        return self

    def all(self):
        return self._records


class _FakeConnection:
    def __init__(self, records):
        self._records = records

    def execute(self, _query):
        return _FakeResult(self._records)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    """Stands in for the MySQL engine, returning canned joined rows."""

    def __init__(self, records):
        self._records = records

    def connect(self):
        return _FakeConnection(self._records)


class _BrokenEngine:
    """Source is down: connecting raises the way SQLAlchemy would."""

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def _source(records):
    return ExternalInventorySource(engine=_FakeEngine(records))


# --------------------------------------------------------------------------- #
# Column mapping
# --------------------------------------------------------------------------- #


def test_maps_a_row_to_the_key_the_catalog_stores():
    (row,) = _source([_record()]).fetch_stock()
    # The catalog stores `cin` namespaced by category (products.external_code),
    # so the reader has to hand back that exact string -- never the vendor's
    # internal `cod`, which is what `binventario.art` actually holds.
    assert row.external_code == "TABLEROS:155"
    assert row.warehouse_code == 1
    assert row.quantity == 558.0


def test_coerces_the_vendor_decimals_and_ints():
    (row,) = _source([_record(bodega=2, cantidad=Decimal("19818.182"))]).fetch_stock()
    assert isinstance(row.quantity, float)
    assert isinstance(row.warehouse_code, int)
    assert row.quantity == pytest.approx(19818.182)


def test_edge_banding_keeps_its_own_namespace():
    (row,) = _source([_record(categoria="TAPACANTOS", codigo="278")]).fetch_stock()
    assert row.external_code == "TAPACANTOS:278"


def test_trims_and_uppercases_the_category():
    (row,) = _source([_record(categoria=" tableros ")]).fetch_stock()
    assert row.external_code == "TABLEROS:155"


def test_a_zero_quantity_is_a_row_like_any_other():
    # Not filtered out: zero is exactly what the low-stock report exists to
    # surface, and on the live catalog 372 of Macas' 412 articles are at zero.
    (row,) = _source([_record(cantidad=Decimal("0.000"))]).fetch_stock()
    assert row.quantity == 0.0


# --------------------------------------------------------------------------- #
# The retirement filter is what makes `external_code` unique
# --------------------------------------------------------------------------- #


def test_retired_articles_are_dropped():
    rows = _source(
        [
            _record(),
            _record(codigo="999", est=0),
            _record(codigo="998", fec_eli="2026-01-01"),
        ]
    ).fetch_stock()
    assert [r.external_code for r in rows] == ["TABLEROS:155"]


def test_two_retired_articles_sharing_a_cin_cannot_collide():
    # The real case: the live catalog has two TAPACANTOS both carrying cin 57,
    # and both are out of service. `cin` is unique only among the LIVE rows, so
    # without the filter these two would claim one `external_code` and whichever
    # was read last would silently win.
    rows = _source(
        [
            _record(categoria="TAPACANTOS", codigo="57", est=0, cantidad=Decimal("1")),
            _record(categoria="TAPACANTOS", codigo="57", est=0, cantidad=Decimal("2")),
            _record(categoria="TAPACANTOS", codigo="278"),
        ]
    ).fetch_stock()
    assert [r.external_code for r in rows] == ["TAPACANTOS:278"]


def test_a_null_est_counts_as_active():
    (row,) = _source([_record(est=None)]).fetch_stock()
    assert row.external_code == "TABLEROS:155"


# --------------------------------------------------------------------------- #
# Unreadable rows and unreachable source
# --------------------------------------------------------------------------- #


def test_an_unreadable_quantity_skips_only_its_row():
    rows = _source(
        [_record(codigo="1", cantidad="n/d"), _record(codigo="2")]
    ).fetch_stock()
    assert [r.external_code for r in rows] == ["TABLEROS:2"]


def test_a_null_warehouse_skips_only_its_row():
    rows = _source(
        [_record(codigo="1", bodega=None), _record(codigo="2")]
    ).fetch_stock()
    assert [r.external_code for r in rows] == ["TABLEROS:2"]


def test_an_unreachable_source_raises_instead_of_reading_empty():
    # Same rule as the catalog and client readers: an empty read is
    # indistinguishable from "the warehouse is empty", and a stock alert that
    # fires on every product because MySQL is down is worse than no alert.
    source = ExternalInventorySource(engine=_BrokenEngine())
    with pytest.raises(ExternalServiceError):
        source.fetch_stock()


def test_an_unconfigured_connection_raises():
    from src.shared.config import config

    original = config.EXTERNAL_CATALOG_URL
    config.EXTERNAL_CATALOG_URL = ""
    try:
        with pytest.raises(ExternalServiceError):
            ExternalInventorySource().fetch_stock()
    finally:
        config.EXTERNAL_CATALOG_URL = original
