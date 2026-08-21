"""Unit: external catalog CSV parsing helpers (no DB).

Covers decoding, header/footer detection, the MEDIO/MEDIA skip token and the
strict board/edge-banding dimension regexes — against both the confirmed
going-forward format and legacy formats that must NOT match (see
``catalog_sync.py``'s module docstring for why guessing is deliberately
avoided rather than "fixed").
"""

import pytest

from src.modules.products.catalog_sync import (
    _BOARD_DIMS_RE,
    _EDGE_DIMS_RE,
    _MEDIO_RE,
    _decode,
    _parse_iva_rate,
    _parse_rows,
    _split_obs,
)
from src.shared.exceptions import BulkValidationError

_HEADER = "CODIGO;ARTICULO;MARCA;TIPO;CATEGORIA;MED;GRUPO;IVA;UNI;CAJ;P.Comp;Tot. Inv;Util%;P.venta;P.venta2;P.Venta3;OBS."


def _csv(*data_rows):
    lines = [
        "Reporte de inventarios;;;;;;;;;;;;;;;;",
        "Para el día jueves, 20 de agosto del 2026;;;;;;;;;;;;;;;;",
        ";;;;;;;;;;;;;;;;",
        _HEADER,
        *data_rows,
        ";;;;;;;;;;TOTAL:;24916.49;;;;;",
    ]
    return "\n".join(lines)


def test_decode_utf8():
    assert _decode("día".encode("utf-8")) == "día"


def test_decode_falls_back_to_cp1252():
    assert _decode("día".encode("cp1252")) == "día"


def test_parse_rows_skips_junk_header_and_total_row():
    text = _csv(
        "1033;PLYWOOD ESTANDAR (2.44X1.22)M-5MM;PELIKANO;PLYWOOD;TABLEROS;Uni;ESTANDAR;15%;60;0;10.8;649.36;35%;14.65;13.47;13.47;"
    )
    rows = _parse_rows(text)
    assert len(rows) == 1
    assert rows[0].codigo == "1033"
    assert rows[0].articulo == "PLYWOOD ESTANDAR (2.44X1.22)M-5MM"
    assert rows[0].row_no == 1


def test_parse_rows_no_header_raises():
    with pytest.raises(BulkValidationError):
        _parse_rows("a;b;c\n1;2;3")


def test_parse_rows_header_not_at_fixed_line():
    # Header two lines later than the usual 3-line junk block still resolves.
    text = "\n".join(
        [
            "x;;;;;;;;;;;;;;;;",
            "y;;;;;;;;;;;;;;;;",
            "z;;;;;;;;;;;;;;;;",
            "w;;;;;;;;;;;;;;;;",
            _HEADER,
            "27;TAPACANTO IBIZA 19X0.40MM;HF;TAPACANTOS;TAPACANTOS;Uni;CANTO MADERADO;15%;100;0;0.18;18;93%;0.35;0.35;0.35;",
        ]
    )
    rows = _parse_rows(text)
    assert len(rows) == 1
    assert rows[0].codigo == "27"


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


def test_parse_iva_rate_blank_means_no_surcharge():
    assert _parse_iva_rate("") == 0.0
    assert _parse_iva_rate("   ") == 0.0


def test_parse_iva_rate_malformed_is_none():
    assert _parse_iva_rate("quince") is None
