"""Unit: external catalog field parsing (no DB).

Covers the MEDIO/MEDIA skip token, the strict board/edge-banding dimension
regexes — against both the confirmed going-forward format and legacy formats
that must NOT match (see ``catalog_sync.py``'s module docstring for why
guessing is deliberately avoided rather than "fixed") — and the OBS./IVA
helpers.

These parse the vendor's free-text columns, so they are independent of where
the rows come from: they outlived the move off the CSV export unchanged.
"""

import pytest

from src.modules.products.catalog_sync import (
    _BOARD_DIMS_RE,
    _EDGE_DIMS_RE,
    _MEDIO_RE,
    _external_code,
    _parse_iva_rate,
    _split_obs,
)


@pytest.mark.parametrize(
    "articulo",
    [
        "MDP RH IBIZA (2.15X2.44)M-15MM (MEDIO)",
        "PELIKANO RH 15MM MATE ROVERE (2.44X2.15)M - MEDIO",
        "MDP RH BLANCO 1.85X2.44 15MM 2C - MEDIA",
        "HIGH GLOSS AMBAR 1.22 X 2.80 X18MM (MEDIA)",
    ],
)
def test_medio_re_matches_both_genders_and_variants(articulo):
    assert _MEDIO_RE.search(articulo)


def test_medio_re_does_not_match_plain_articulo():
    assert not _MEDIO_RE.search("MDP RH ROBLE BARROCO AMBAR (2.07X2.80)M-15MM")


def test_board_dims_re_matches_confirmed_format():
    m = _BOARD_DIMS_RE.search("MDP RH ROBLE BARROCO AMBAR (2.07X2.80)M-15MM")
    assert m.groups() == ("2.07", "2.80", "15")


@pytest.mark.parametrize(
    "articulo",
    [
        "PLYWOOD ESTÁNDAR (244X122)X5MM",  # truncated-meters legacy format
        "PINO NATURAL 1200X2400X45MM",  # bare mm, no parens
        "MDP RH ROBLE JAPANDI (1.85X2.75)-15MM",  # missing "M" before hyphen
        "MDP WENGUE (1.83X2.44) 36MM",  # thickness after a space, no hyphen
        "PLYWOOD (PELIKANO) 06MM ESTANDAR",  # no width/height at all
    ],
)
def test_board_dims_re_deliberately_rejects_legacy_formats(articulo):
    assert _BOARD_DIMS_RE.search(articulo) is None


@pytest.mark.parametrize(
    "articulo, width, thickness",
    [
        ("TAPACANTO IBIZA 19X0.40MM", "19", "0.40"),
        ("PVC LILA 22X0.40MM", "22", "0.40"),
        ("TAPACANTO PLOMO 45X1MM", "45", "1"),
        ("TAPACANTO CEDRO 40X1.5MM", "40", "1.5"),
    ],
)
def test_edge_dims_re_matches_confirmed_format(articulo, width, thickness):
    m = _EDGE_DIMS_RE.search(articulo)
    assert m.groups() == (width, thickness)


def test_edge_dims_re_rejects_missing_unit():
    assert _EDGE_DIMS_RE.search("TAPACANTO CAPRI 22X1.5") is None


def test_split_obs_with_separator():
    assert _split_obs("CSH - Cashmere") == ("CSH", "Cashmere")


def test_split_obs_empty():
    assert _split_obs("") == (None, None)


def test_split_obs_without_separator():
    assert _split_obs("Cashmere") == (None, None)


def test_parse_iva_rate_percent():
    assert _parse_iva_rate("15%") == 0.15


def test_parse_iva_rate_no_percent_sign():
    assert _parse_iva_rate("15") == 0.15


def test_parse_iva_rate_decimal_column_form():
    # How MySQL's decimal(5,2) renders: no percent sign, trailing zeros.
    assert _parse_iva_rate("15.00") == 0.15
    assert _parse_iva_rate("0.00") == 0.0


def test_parse_iva_rate_blank_means_no_surcharge():
    assert _parse_iva_rate("") == 0.0
    assert _parse_iva_rate("   ") == 0.0


def test_parse_iva_rate_malformed_is_none():
    assert _parse_iva_rate("quince") is None


def test_external_code_is_namespaced_by_category():
    # The key the sync matches on; a board and an edge banding may share a bare
    # code in the vendor's system without colliding here.
    assert _external_code("TABLEROS", "1033") == "TABLEROS:1033"
    assert _external_code("TAPACANTOS", "1033") == "TAPACANTOS:1033"
