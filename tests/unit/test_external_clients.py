"""Unit: the external client reader (no MySQL, no DB).

Covers the column mapping (``mcliente`` row -> ``ClientSourceRow``), the type
coercion the validator depends on, the active/retired split driven by ``est``,
and the deliberate refusal to degrade quietly when the source is unreachable —
the same contract as ``test_external_catalog.py``, for the sibling table.
"""

import pytest
from sqlalchemy.exc import OperationalError

from src.modules.clients.external_clients import ExternalClientsSource
from src.shared.exceptions import ExternalServiceError


def _record(**overrides):
    """One ``mcliente`` row with realistic vendor types (int/None)."""
    base = {
        "ced": "1400171417",
        "nom": "JHOANA PATRICIA GUAMAN VAZQUEZ",
        "mai": "jhoana@gmail.com",
        "tel": "0981105628",
        "tel2": None,
        "est": 1,
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
    """Stands in for the MySQL engine, returning canned ``mcliente`` rows."""

    def __init__(self, records):
        self._records = records

    def connect(self):
        return _FakeConnection(self._records)


class _BrokenEngine:
    """Source is down: connecting raises the way SQLAlchemy would."""

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def _source(records):
    return ExternalClientsSource(engine=_FakeEngine(records))


def test_maps_every_column_the_sync_needs():
    active, retired = _source([_record()]).fetch_rows()
    assert retired == []
    (row,) = active
    assert row.row_no == 1
    assert row.cedula == "1400171417"
    assert row.nombre == "JHOANA PATRICIA GUAMAN VAZQUEZ"
    assert row.email == "jhoana@gmail.com"
    assert row.telefono == "0981105628"


def test_null_becomes_blank_not_the_string_none():
    """A blank reads as a missing field; "None" would surface as a baffling
    warning about an e-mail nobody wrote."""
    (row,), _ = _source([_record(mai=None, tel=None, tel2=None)]).fetch_rows()
    assert row.email == ""
    assert row.telefono == ""
    assert row.telefono2 == ""


def test_non_text_columns_are_coerced():
    """The driver can hand back an int for a numeric-looking phone."""
    (row,), _ = _source([_record(tel=72740486)]).fetch_rows()
    assert row.telefono == "72740486"


def test_values_are_trimmed():
    (row,), _ = _source([_record(nom="  JUAN PEREZ  ")]).fetch_rows()
    assert row.nombre == "JUAN PEREZ"


def test_row_no_is_the_read_position():
    active, _ = _source([_record(ced="1"), _record(ced="2")]).fetch_rows()
    assert [row.row_no for row in active] == [1, 2]


@pytest.mark.parametrize(
    "est,retired",
    [(1, False), (None, False), (0, True), (2, True), ("0", True), ("x", False)],
)
def test_est_decides_the_split(est, retired):
    """A NULL ``est`` counts as active: 1 is the column default, so NULL
    carries no retirement intent."""
    active, retired_rows = _source([_record(est=est)]).fetch_rows()
    assert bool(retired_rows) is retired
    assert bool(active) is not retired


def test_retired_rows_are_returned_not_dropped():
    """They are counted into the report, so a shrinking import has a reason."""
    active, retired = _source(
        [_record(ced="1"), _record(ced="2", est=0), _record(ced="3")]
    ).fetch_rows()
    assert [row.cedula for row in active] == ["1", "3"]
    assert [row.cedula for row in retired] == ["2"]


def test_an_unreachable_source_raises_instead_of_reading_empty():
    """Never degrade silently: "no clients" and "the database is down" must not
    look the same to the sync."""
    source = ExternalClientsSource(engine=_BrokenEngine())
    with pytest.raises(ExternalServiceError):
        source.fetch_rows()


def test_an_unconfigured_url_raises(monkeypatch):
    from src.shared import external_db

    monkeypatch.setattr(external_db.config, "EXTERNAL_CATALOG_URL", "")
    with pytest.raises(ExternalServiceError):
        ExternalClientsSource().fetch_rows()
