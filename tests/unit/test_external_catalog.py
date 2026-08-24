"""Unit: the external inventory reader (no MySQL, no DB).

Covers the column mapping (``marticulo`` row -> ``SourceRow``), the type
coercion the validator depends on, the active/retired split driven by
``est``/``FecEli``, and the deliberate refusal to degrade quietly when the
source is unreachable.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from src.modules.products.external_catalog import (
    ExternalCatalogSource,
    _check_url,
    _is_retired,
    _text,
)
from src.shared.exceptions import ExternalServiceError

_COLUMNS = (
    "cin",
    "nom",
    "mar",
    "tip",
    "cat",
    "gru",
    "iva",
    "ven",
    "obs",
    "est",
    "FecEli",
)


def _record(**overrides):
    """One ``marticulo`` row with realistic vendor types (Decimal/int/None)."""
    base = {
        "cin": "1033",
        "nom": "MDP RH ROBLE BARROCO AMBAR (2.07X2.80)M-15MM",
        "mar": "KRONOSPAN",
        "tip": "MDP",
        "cat": "TABLEROS",
        "gru": "MDP MELAMINA RH",
        "iva": Decimal("15.00"),
        "ven": Decimal("14.650000"),
        "obs": "Cashmere - CSH",
        "est": 1,
        "FecEli": None,
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
    """Stands in for the MySQL engine, returning canned ``marticulo`` rows."""

    def __init__(self, records):
        self._records = records

    def connect(self):
        return _FakeConnection(self._records)


class _BrokenEngine:
    """Source is down: connecting raises the way SQLAlchemy would."""

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def _source(records):
    return ExternalCatalogSource(engine=_FakeEngine(records))


# --------------------------------------------------------------------------- #
# Column mapping and coercion
# --------------------------------------------------------------------------- #


def test_maps_every_column_the_validator_needs():
    active, retired = _source([_record()]).fetch_rows()
    assert retired == []
    (row,) = active
    assert row.row_no == 1
    assert row.codigo == "1033"
    assert row.articulo == "MDP RH ROBLE BARROCO AMBAR (2.07X2.80)M-15MM"
    assert row.marca == "KRONOSPAN"
    assert row.tipo == "MDP"
    assert row.categoria == "TABLEROS"
    assert row.grupo == "MDP MELAMINA RH"
    assert row.obs == "Cashmere - CSH"


def test_numeric_columns_reach_the_validator_as_text():
    # decimal(5,2) and decimal(15,6) must survive as parseable strings: the
    # validator was written against a text export and calls float() on them.
    (row,), _ = _source([_record()]).fetch_rows()
    assert row.iva == "15.00"
    assert row.p_venta == "14.650000"
    assert float(row.p_venta) == pytest.approx(14.65)


def test_null_columns_become_blank_not_the_string_none():
    # "None" would surface as a baffling "precio de venta 'None' no es un
    # número válido"; "" reads as a missing field.
    (row,), _ = _source([_record(obs=None, mar=None, gru=None)]).fetch_rows()
    assert row.obs == ""
    assert row.marca == ""
    assert row.grupo == ""


def test_whitespace_is_trimmed():
    (row,), _ = _source([_record(cat="  TABLEROS  ")]).fetch_rows()
    assert row.categoria == "TABLEROS"


def test_row_no_counts_read_position():
    records = [_record(cin=1), _record(cin=2, nom="B"), _record(cin=3, nom="C")]
    active, _ = _source(records).fetch_rows()
    assert [r.row_no for r in active] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Active / retired split
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "est, fec_eli, expected",
    [
        (1, None, False),
        (0, None, True),
        (2, None, True),
        (1, date(2026, 5, 1), True),  # FecEli wins over an active flag
        (None, None, False),  # NULL est carries no retirement intent
        ("1", None, False),  # driver may hand back a string
        ("basura", None, False),  # unparseable: don't remove a product
    ],
)
def test_is_retired(est, fec_eli, expected):
    assert _is_retired(est, fec_eli) is expected


def test_retired_rows_are_split_out_not_dropped():
    records = [
        _record(cin=1, nom="ACTIVO (2.07X2.80)M-15MM"),
        _record(cin=2, nom="DE BAJA (2.07X2.80)M-15MM", est=0),
        _record(cin=3, nom="ELIMINADO (2.07X2.80)M-15MM", FecEli=date(2026, 5, 1)),
    ]
    active, retired = _source(records).fetch_rows()
    assert [r.codigo for r in active] == ["1"]
    assert [r.codigo for r in retired] == ["2", "3"]


def test_retired_row_with_unparseable_name_is_still_returned():
    # It never reaches validation, so malformed data on a product nobody sells
    # any more must not be able to abort the sync.
    records = [
        _record(cin=1, nom="ACTIVO (2.07X2.80)M-15MM"),
        _record(cin=2, nom="SIN MEDIDAS NI FORMATO", est=0),
    ]
    active, retired = _source(records).fetch_rows()
    assert len(active) == 1
    assert len(retired) == 1


# --------------------------------------------------------------------------- #
# Failure modes: this source must never degrade quietly
# --------------------------------------------------------------------------- #


def test_unreachable_source_raises_instead_of_returning_empty():
    # An empty read is indistinguishable from "the vendor deleted everything",
    # which would make the sync's reconciliation wipe our catalog.
    source = ExternalCatalogSource(engine=_BrokenEngine())
    with pytest.raises(ExternalServiceError):
        source.fetch_rows()


def test_unconfigured_url_raises(monkeypatch):
    from src.shared import config as config_module

    monkeypatch.setattr(config_module.config, "EXTERNAL_CATALOG_URL", "")
    with pytest.raises(ExternalServiceError):
        ExternalCatalogSource().fetch_rows()


def test_text_helper():
    assert _text(None) == ""
    assert _text(Decimal("0.350000")) == "0.350000"
    assert _text(57) == "57"
    assert _text("  hola  ") == "hola"


# --------------------------------------------------------------------------- #
# URL sanity: an unencoded password must never reach the driver
# --------------------------------------------------------------------------- #

_GOOD = "mysql+pymysql://user:%40secreto%40@10.0.0.1:33061/inv?charset=latin1"


def test_check_url_accepts_a_properly_encoded_password():
    _check_url(_GOOD)


@pytest.mark.parametrize(
    "url",
    [
        # SQLAlchemy splits at the FIRST '@', so one inside the password leaves
        # the rest of it as the host: the driver then reports "Name or service
        # not known" for a name that is really the credential.
        "mysql+pymysql://user:@secreto@@10.0.0.1:33061/inv",
        "mysql+pymysql://user:secreto@@10.0.0.1:33061/inv",
        "no-es-una-url",
    ],
)
def test_check_url_rejects_unencoded_credentials(url):
    with pytest.raises(ExternalServiceError):
        _check_url(url)


def test_check_url_allows_a_slash_in_the_password():
    """Only '@' actually breaks the split, so don't reject what works: a lone
    slash leaves a single '@' and parses correctly."""
    _check_url("mysql+pymysql://user:pass/word@10.0.0.1:33061/inv")


def test_malformed_url_error_never_echoes_the_password(monkeypatch):
    """The whole point of checking early: the driver's own error message
    embeds the password, and anything logging it leaks the credential."""
    secret = "ArticulosMaderable"
    from src.shared import config as config_module

    monkeypatch.setattr(
        config_module.config,
        "EXTERNAL_CATALOG_URL",
        f"mysql+pymysql://user:@{secret}@@45.225.44.59:33061/inv",
    )
    with pytest.raises(ExternalServiceError) as exc:
        ExternalCatalogSource().fetch_rows()
    assert secret not in str(exc.value)
    assert "%40" in str(exc.value)
