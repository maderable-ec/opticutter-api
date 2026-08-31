"""Unit: external catalog field parsing and coordination warnings (no DB).

Covers the MEDIO/MEDIA skip token, the strict board/edge-banding dimension
regexes — against both the confirmed going-forward format and legacy formats
that must NOT match (see ``catalog_sync.py``'s module docstring for why
guessing is deliberately avoided rather than "fixed") — the OBS./IVA helpers,
and ``_collect_warnings``, which reports the rows that imported but lost their
board<->tapacanto coordination.

These parse the vendor's free-text columns, so they are independent of where
the rows come from: they outlived the move off the CSV export unchanged.
"""

import pytest

from src.modules.products.catalog_sync import (
    _BOARD_DIMS_RE,
    _EDGE_DIMS_RE,
    _MEDIO_RE,
    _collect_warnings,
    _external_code,
    _parse_iva_rate,
    _parse_obs,
    _ValidRow,
)
from src.modules.products.model import ProductType


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


@pytest.mark.parametrize(
    "obs, expected",
    [
        # The minimum case, and the reason the order was inverted: a board has
        # no alias to write, so the family alone has to be valid input. Under
        # the old "ALIAS - FAMILIA" this yielded (None, None) and the board
        # silently lost its coordination key.
        ("Cashmere", ("Cashmere", None)),
        ("Cashmere - CSH", ("Cashmere", "CSH")),
        ("  Cashmere - CSH  ", ("Cashmere", "CSH")),
        # Split on the LAST separator, so a hyphen inside the design's own name
        # stays on the family side.
        ("Roble Barroco - Dorado - RBD", ("Roble Barroco - Dorado", "RBD")),
        # An alias is a short code: whitespace on the right means this is a
        # hyphenated family, not an alias. Guards the proforma's "Cantos"
        # column, which is sized for "2L1C CS CSH".
        ("Cashmere - Cashmere Claro", ("Cashmere - Cashmere Claro", None)),
        # ...and so does the 20-char cap (EdgeBandingAttributes.alias): over it,
        # the text is family, not a truncated alias and not a skipped row.
        ("Cashmere - " + "X" * 21, ("Cashmere - " + "X" * 21, None)),
        ("Cashmere - " + "X" * 20, ("Cashmere", "X" * 20)),
        # The separator is " - ", spaces included: a bare hyphen is part of the
        # name.
        ("Cashmere-CSH", ("Cashmere-CSH", None)),
        # The old workaround for a board (lead with the separator so the
        # family lands on the right) is now just a family that starts with a
        # hyphen — it won't match a board written "Cashmere", which the
        # sync's warnings are there to surface.
        (" - CSH", ("- CSH", None)),
        ("", (None, None)),
        ("   ", (None, None)),
    ],
)
def test_parse_obs(obs, expected):
    assert _parse_obs(obs) == expected


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


# --- Coordination warnings ----------------------------------------------------
# Every one of these rows imports fine; what they've lost is the family match
# that powers GET /products/{board_id}/edge-bandings, which fails silently.
def _row(
    product_type,
    row_no=1,
    family=None,
    alias=None,
    name=None,
    height=None,
    width=None,
    thickness=None,
    price=10.0,
    price_2=9.0,
    price_3=8.0,
    iva_rate=0.15,
):
    attributes = {}
    if family is not None:
        attributes["family"] = family
    if alias is not None:
        attributes["alias"] = alias
    if height is not None:
        attributes["height"] = height
    if width is not None:
        attributes["width"] = width
    if thickness is not None:
        attributes["thickness"] = thickness
    return _ValidRow(
        row_no=row_no,
        codigo=str(1000 + row_no),
        external_code=f"CAT:{1000 + row_no}",
        product_type=product_type,
        name=name or f"ARTICULO {row_no}",
        description=None,
        price=price,
        price_2=price_2,
        price_3=price_3,
        iva_rate=iva_rate,
        attributes=attributes,
    )


def _board(**kwargs):
    return _row(ProductType.BOARD, **kwargs)


def _banding(**kwargs):
    return _row(ProductType.EDGE_BANDING, **kwargs)


def _messages(rows, tax_rate=0.15):
    return [w.message for w in _collect_warnings(rows, tax_rate)]


def test_coordinated_pair_warns_nothing():
    rows = [
        _board(family="Cashmere"),
        _banding(row_no=2, family="Cashmere", alias="CSH"),
    ]
    assert _collect_warnings(rows, 0.15) == []


def test_family_match_is_case_and_space_insensitive():
    # Same normalization as the coordination query, or the warning would fire
    # on pairs that DO match.
    rows = [
        _board(family="CASHMERE"),
        _banding(row_no=2, family=" cashmere ", alias="CSH"),
    ]
    assert _collect_warnings(rows, 0.15) == []


def test_board_without_family_is_not_a_warning():
    # Plywood/OSB/MDF fondo have no coordinated banding at all; warning about
    # every one of them would bury the real problems.
    assert _collect_warnings([_board()], 0.15) == []


def test_board_with_longer_side_first_warns_nothing():
    assert _collect_warnings([_board(height=2800, width=2070)], 0.15) == []


def test_board_with_equal_sides_warns_nothing():
    assert _collect_warnings([_board(height=2000, width=2000)], 0.15) == []


def test_board_with_shorter_side_first_is_warned():
    (message,) = _messages([_board(height=2070, width=2800)])
    assert "largo" in message and "ancho" in message


def test_edge_banding_without_family_is_warned():
    (message,) = _messages([_banding(family=None, alias="CSH")])
    assert "sin familia" in message


def test_edge_banding_without_alias_is_warned():
    rows = [_board(family="Cashmere"), _banding(row_no=2, family="Cashmere")]
    (message,) = _messages(rows)
    assert "sin alias" in message


def test_missing_family_supersedes_missing_alias():
    # One row, one reason: with no family the alias is the lesser problem.
    messages = _messages([_banding()])
    assert len(messages) == 1
    assert "sin familia" in messages[0]


def test_board_family_without_any_banding_is_warned():
    rows = [_board(family="Cashmere"), _banding(row_no=2, family="Ibiza", alias="IBZ")]
    messages = _messages(rows)
    assert any("'Cashmere' solo aparece en tableros" in m for m in messages)
    assert any("'Ibiza' solo aparece en tapacantos" in m for m in messages)


def test_orphan_family_is_reported_once_not_per_row():
    # Anchored to the first article that declared it: a family on 30 boards is
    # one line, not 30.
    rows = [_board(row_no=n, family="Cashmere") for n in range(1, 4)]
    warnings = _collect_warnings(rows, 0.15)
    assert len(warnings) == 1
    assert warnings[0].row_no == 1


def _pair(board_thickness, *banding_widths, family="Cashmere"):
    """A coordinated family: one board plus a banding per stocked width."""
    return [_board(family=family, thickness=board_thickness)] + [
        _banding(row_no=n + 2, family=family, alias="CSH", width=w, thickness=0.45)
        for n, w in enumerate(banding_widths)
    ]


def test_a_covering_width_warns_nothing():
    assert _collect_warnings(_pair(15, 22, 40), 0.15) == []


def test_family_with_no_width_that_covers_the_board_is_warned():
    # The real case: a 36mm board whose design is only stocked in 19mm tape.
    # Its picker is as empty as a board with a broken family, so it's reported
    # the same way.
    (message,) = _messages(_pair(36, 19, 22))
    assert "no tiene ningún tapacanto que cubra un tablero de 36mm" in message
    assert "solo hay de 19/22mm" in message


def test_width_gap_is_reported_per_thickness_not_per_family():
    # A design can coordinate at 15mm and have nothing for its 36mm sibling.
    rows = _pair(15, 19) + [_board(row_no=9, family="Cashmere", thickness=36)]
    (warning,) = _collect_warnings(rows, 0.15)
    assert warning.row_no == 9
    assert "de 36mm" in warning.message


def test_width_gap_is_reported_once_per_thickness():
    rows = _pair(36, 19) + [
        _board(row_no=n, family="Cashmere", thickness=36) for n in (8, 9)
    ]
    warnings = _collect_warnings(rows, 0.15)
    assert len(warnings) == 1
    assert warnings[0].row_no == 1  # anchored to the first board that needs it


def test_family_without_any_banding_is_not_also_reported_as_a_width_gap():
    # One row, one reason: the orphan-family warning already says it.
    messages = _messages([_board(family="Cashmere", thickness=15)])
    assert len(messages) == 1
    assert "solo aparece en tableros" in messages[0]


def test_edge_banding_thicker_than_it_is_wide_is_warned():
    # "18X45MM" where the siblings say "18X0.45MM": the vendor dropped a
    # decimal point, and the thickness is what band type is inferred from.
    rows = [
        _board(family="Azul Urbano", thickness=15),
        _banding(row_no=2, family="Azul Urbano", alias="AUR", width=18, thickness=45.0),
    ]
    (message,) = _messages(rows)
    assert "espesor (45mm) es mayor que el ancho (18mm)" in message


def test_an_implausible_thickness_does_not_discard_the_row_width():
    # Only the thickness is broken; dropping its width from the coverage check
    # would invent a second, false warning about a family that does coordinate.
    rows = [
        _board(family="Azul Urbano", thickness=15),
        _banding(row_no=2, family="Azul Urbano", alias="AUR", width=18, thickness=45.0),
    ]
    messages = _messages(rows)
    assert len(messages) == 1
    assert "cubra un tablero" not in messages[0]


def test_warnings_are_ordered_by_row():
    rows = [
        _banding(row_no=3, family="Ibiza", alias="IBZ"),
        _banding(row_no=1),
        _board(row_no=2, family="Cashmere"),
    ]
    assert [w.row_no for w in _collect_warnings(rows, 0.15)] == [1, 2, 3]
