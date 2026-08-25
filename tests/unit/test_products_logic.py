"""Unit: board<->edge-banding coordination in ``ProductService`` (no DB).

The width rule is pure logic; the matching in ``find_edge_bandings_for_board``
is tested with ``mock_session`` returning fake candidates, without touching the
real catalog. Boards and edge bandings are paired by an explicit, configurable
``family`` attribute (not the editable code).
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.modules.products.service import (
    ProductService,
    edge_width_fits_board,
    normalize_family,
)
from src.modules.products.types.board import BoardAttributes
from src.modules.products.types.edge_banding import BandType
from src.shared.exceptions import BusinessRuleError


# --- Pure logic ---------------------------------------------------------------
@pytest.mark.parametrize(
    "thickness,width",
    [
        # The widths the vendor actually stocks for each board thickness.
        (15, 18),
        (15, 19),
        (15, 20),
        (15, 22),
        (16, 20),
        (18, 19),
        (18, 22),
        (36, 40),
        (36, 45),
    ],
)
def test_edge_width_covers_the_board(thickness, width):
    assert edge_width_fits_board(thickness, width) is True


@pytest.mark.parametrize(
    "thickness,width,why",
    [
        (15, 15, "no overhang left to trim"),
        (15, 12, "narrower than the board"),
        (15, 40, "a 36mm tape wastes most of itself on a 15mm board"),
        (15, 35, "still far too wide"),
        (36, 22, "narrower than the board"),
        (36, 19, "narrower than the board"),
        (36, 50, "past the overhang the trimmer takes"),
        (6, 18, "no tape narrow enough for a fondo"),
    ],
)
def test_edge_width_does_not_cover_the_board(thickness, width, why):
    assert edge_width_fits_board(thickness, width) is False, why


def test_edge_width_rule_handles_fractional_thickness():
    """The rule reads the thickness as a float instead of truncating it, so a
    15.8mm board no longer passes for a 15mm one."""
    assert edge_width_fits_board(15.8, 19) is True
    assert edge_width_fits_board(15.8, 16) is False  # only 0.2mm of overhang


@pytest.mark.parametrize(
    "value,expected",
    [
        ("CASHMERE", "cashmere"),
        ("  Cashmere  ", "cashmere"),
        (None, ""),
        ("", ""),
    ],
)
def test_norm_family(value, expected):
    assert normalize_family(value) == expected


# --- Matching with a mocked session --------------------------------------------
def _board(family="CASHMERE", thickness=15):
    return SimpleNamespace(
        id=1,
        code="MDP-SL-CSH-15",
        type="board",
        attributes={"thickness": thickness, "family": family},
    )


_next_id = iter(range(100, 1000))


def _band(code, width, *, family="CASHMERE", band_type="Soft", thickness=0.45):
    return SimpleNamespace(
        id=next(_next_id),
        code=code,
        type="edge_banding",
        attributes={
            "width": width,
            "bandType": band_type,
            "thickness": thickness,
            "family": family,
        },
    )


def _candidates(mock_session, items):
    mock_session.query.return_value.filter.return_value.all.return_value = items


def test_matches_by_family_and_thickness_width(mock_session):
    mock_session.get.return_value = _board()  # 15mm board
    _candidates(
        mock_session,
        [
            _band("TAP-SL-CSH-019", 19),  # matches
            _band("TAP-SL-CSH-040", 40),  # too wide for 15mm
            _band("TAP-OT-XXX-019", 19, family="BARROCO"),  # different family
        ],
    )
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == ["TAP-SL-CSH-019"]


def test_every_stocked_width_that_covers_the_board_is_returned(mock_session):
    """The real catalog stocks a design in several widths (19 AND 22 for a
    15mm board). Returning only one of them left the seller picking by hand
    from the whole catalog; both belong in the list, narrowest first."""
    mock_session.get.return_value = _board()
    _candidates(
        mock_session,
        [
            _band("TAP-CSH-022-D", 22, band_type="Hard", thickness=1.5),
            _band("TAP-CSH-019-D", 19, band_type="Hard", thickness=1.5),
            _band("TAP-CSH-022-S", 22, thickness=0.45),
            _band("TAP-CSH-019-S", 19, thickness=0.40),
        ],
    )
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == [
        "TAP-CSH-019-S",
        "TAP-CSH-019-D",
        "TAP-CSH-022-S",
        "TAP-CSH-022-D",
    ]


def test_a_design_stocked_only_in_the_wider_width_still_coordinates(mock_session):
    """The case that broke first on the real catalog: a 15mm Cashmere board
    whose design only comes in 22mm used to return an empty picker."""
    mock_session.get.return_value = _board()
    _candidates(mock_session, [_band("TAP-CSH-022", 22, thickness=1.0)])
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == ["TAP-CSH-022"]


def test_off_table_thickness_coordinates(mock_session):
    """16mm boards (GRAFFO/CINZA) and their 20mm tape were invisible while the
    rule was a table keyed on 15 and 36."""
    mock_session.get.return_value = _board(thickness=16)
    _candidates(
        mock_session,
        [_band("TAP-GRF-020", 20), _band("TAP-GRF-040", 40)],
    )
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == ["TAP-GRF-020"]


def test_a_36mm_board_never_sees_the_narrow_tape(mock_session):
    """The upper bound is not the only one that matters: a 19mm tape cannot
    cover a 36mm edge, and its design stocking one is a real catalog gap."""
    mock_session.get.return_value = _board(thickness=36)
    _candidates(
        mock_session,
        [_band("TAP-CRB-019", 19), _band("TAP-CRB-022", 22)],
    )
    assert ProductService(mock_session).find_edge_bandings_for_board(1) == []


def test_family_match_is_case_insensitive(mock_session):
    mock_session.get.return_value = _board(family="Cashmere")
    _candidates(mock_session, [_band("TAP-SL-CSH-019", 19, family="  cashmere ")])
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == ["TAP-SL-CSH-019"]


def test_board_without_family_returns_empty(mock_session):
    mock_session.get.return_value = _board(family=None)
    _candidates(mock_session, [_band("TAP-SL-CSH-019", 19)])
    assert ProductService(mock_session).find_edge_bandings_for_board(1) == []


def test_filters_by_band_type(mock_session):
    mock_session.get.return_value = _board()
    _candidates(
        mock_session,
        [
            _band("TAP-SL-CSH-019", 19, band_type="Soft"),
            _band("TAP-SL-CSH-019D", 19, band_type="Hard"),
        ],
    )
    result = ProductService(mock_session).find_edge_bandings_for_board(
        1, band_type=BandType.SOFT
    )
    assert [p.code for p in result] == ["TAP-SL-CSH-019"]


def test_non_board_product_is_rejected(mock_session):
    mock_session.get.return_value = SimpleNamespace(
        code="TAP-SL-CSH-019", type="edge_banding", attributes={}
    )
    with pytest.raises(BusinessRuleError):
        ProductService(mock_session).find_edge_bandings_for_board(1)


# --------------------------------------------------------------------------- #
# Board thickness accepts the fractional values the vendor actually sells
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("thickness", [15, 18, 5.5, 9.5, 11.1, 18.3, 3.6])
def test_board_thickness_accepts_whole_and_fractional_mm(thickness):
    """OSB, MDF fondo and thin plywood ship in fractional thicknesses and are
    in the external catalog; an integer-only field silently kept them out."""
    attrs = BoardAttributes(height=2440, width=1220, thickness=thickness)
    assert attrs.thickness == pytest.approx(float(thickness))


@pytest.mark.parametrize("thickness", [0, -1, -0.5])
def test_board_thickness_still_rejects_impossible_values(thickness):
    with pytest.raises(ValidationError):
        BoardAttributes(height=2440, width=1220, thickness=thickness)


def test_fractional_thickness_coordinates_no_edge_banding(mock_session):
    """A 5.5mm MDF fondo has no coordinated tapacanto: the narrowest tape the
    vendor stocks is 18mm, way past the overhang the trimmer takes."""
    svc = ProductService(mock_session)
    mock_session.get.return_value = _board(thickness=5.5)
    _candidates(
        mock_session, [_band("TAP-SL-CSH-018", 18), _band("TAP-SL-CSH-019", 19)]
    )
    assert svc.find_edge_bandings_for_board(1) == []
