"""Unit: board<->edge-banding coordination in ``ProductService`` (no DB).

The thickness->width map is pure logic; the matching in
``find_edge_bandings_for_board`` is tested with ``mock_session`` returning fake
candidates, without touching the real catalog. Boards and edge bandings are
paired by an explicit, configurable ``family`` attribute (not the editable code).
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.modules.products.service import (
    BOARD_THICKNESS_TO_EDGE_WIDTH,
    ProductService,
)
from src.modules.products.types.board import BoardAttributes
from src.modules.products.types.edge_banding import BandType
from src.shared.exceptions import BusinessRuleError


# --- Pure logic ---------------------------------------------------------------
def test_thickness_to_edge_width_map():
    assert BOARD_THICKNESS_TO_EDGE_WIDTH[15] == 19
    assert BOARD_THICKNESS_TO_EDGE_WIDTH[36] == 40


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
    assert ProductService._norm_family(value) == expected


# --- Matching with a mocked session --------------------------------------------
def _board(family="CASHMERE", thickness=15):
    return SimpleNamespace(
        code="MDP-SL-CSH-15",
        type="board",
        attributes={"thickness": thickness, "family": family},
    )


def _band(code, width, *, family="CASHMERE", band_type="Soft", thickness=0.45):
    return SimpleNamespace(
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
    mock_session.get.return_value = _board()  # 15mm board => target width 19
    _candidates(
        mock_session,
        [
            _band("TAP-SL-CSH-019", 19),  # matches
            _band("TAP-SL-CSH-040", 40),  # wrong width for 15mm
            _band("TAP-OT-XXX-019", 19, family="BARROCO"),  # different family
        ],
    )
    result = ProductService(mock_session).find_edge_bandings_for_board(1)
    assert [p.code for p in result] == ["TAP-SL-CSH-019"]


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
    """A 5.5mm board has no coordinated tapacanto, and the lookup must return
    an empty list rather than blow up on the int() the width rule applies."""
    svc = ProductService(mock_session)
    mock_session.get.return_value = _board(thickness=5.5)
    assert svc.find_edge_bandings_for_board(1) == []
