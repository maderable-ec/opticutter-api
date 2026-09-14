"""Unit: which boards are sold as halves, and along which axis (no DB, no engine).

The shop does not halve every material and does not halve them all the same
way: MDP of 36, OSB, pino, madera natural, high gloss, matt soft and enchapado
are only ever sold whole, plywood and MDF ranurado are ripped parallel to the
lado corto, and everything else keeps the parallel-to-the-largo cut every board
used to get. The rule is derived from the subtype the catalog sync already
fills from the vendor's own ``TIPO``, so these tests pin the table itself plus
the one place that turns it into geometry (``_half_spec``).
"""

import pytest

from src.modules.optimizations.materials import ResolvedMaterial
from src.modules.optimizations.schemas import MaterialSource
from src.modules.optimizations.service import OptimizationService
from src.modules.products.types.board import (
    BoardSubtype,
    HalfBoardSplit,
    half_board_split,
)

MARKUP = 0.10
WIDTH = 2070.0
HEIGHT = 2800.0
COST = 80.0


def _material(split: HalfBoardSplit, source=MaterialSource.catalog.value):
    return ResolvedMaterial(
        key="b1",
        width=WIDTH,
        height=HEIGHT,
        thickness=15.0,
        cost_per_unit=COST,
        source=source,
        half_split=split,
    )


# --- the policy table -----------------------------------------------------------
@pytest.mark.parametrize(
    "subtype,expected",
    [
        (BoardSubtype.MDP, HalfBoardSplit.LONG_SIDE),
        (BoardSubtype.MDF, HalfBoardSplit.LONG_SIDE),
        (BoardSubtype.HDF, HalfBoardSplit.LONG_SIDE),
        (BoardSubtype.PLYWOOD, HalfBoardSplit.SHORT_SIDE),
        (BoardSubtype.GROOVED, HalfBoardSplit.SHORT_SIDE),
        (BoardSubtype.OSB, HalfBoardSplit.NONE),
        (BoardSubtype.PINE, HalfBoardSplit.NONE),
        (BoardSubtype.NATURAL_WOOD, HalfBoardSplit.NONE),
        (BoardSubtype.HIGH_GLOSS, HalfBoardSplit.NONE),
        (BoardSubtype.MATH_SOFT, HalfBoardSplit.NONE),
        (BoardSubtype.VENEER, HalfBoardSplit.NONE),
    ],
)
def test_every_subtype_has_a_policy(subtype, expected):
    """Every member of the closed enum, so a new one can't be added in silence."""
    assert half_board_split(subtype, 15.0) is expected


def test_the_whole_subtype_enum_is_covered():
    assert {s for s in BoardSubtype} == {
        BoardSubtype.MDP,
        BoardSubtype.MDF,
        BoardSubtype.HDF,
        BoardSubtype.PLYWOOD,
        BoardSubtype.GROOVED,
        BoardSubtype.OSB,
        BoardSubtype.PINE,
        BoardSubtype.NATURAL_WOOD,
        BoardSubtype.HIGH_GLOSS,
        BoardSubtype.MATH_SOFT,
        BoardSubtype.VENEER,
    }


def test_thick_mdp_is_never_halved():
    """The 36mm MDP: 35 boards in the live catalog, and the vendor sells no
    "(MEDIO)" SKU for a single one of them."""
    assert half_board_split(BoardSubtype.MDP, 36.0) is HalfBoardSplit.NONE
    assert half_board_split(BoardSubtype.MDP, 36) is HalfBoardSplit.NONE


def test_the_thickness_rule_is_mdp_only():
    """Decided with the shop: it is the MDP of 36 that isn't sold in halves, not
    "anything thick" — an MDF that arrives at 36 still gets its half."""
    assert half_board_split(BoardSubtype.MDF, 36.0) is HalfBoardSplit.LONG_SIDE
    assert half_board_split(BoardSubtype.MDP, 25.0) is HalfBoardSplit.LONG_SIDE
    assert half_board_split(BoardSubtype.MDP, 18.0) is HalfBoardSplit.LONG_SIDE


def test_the_raw_string_from_the_catalog_is_accepted():
    """``attributes`` is a JSON bag: the subtype arrives as text, not as the enum."""
    assert half_board_split("Plywood", 15.0) is HalfBoardSplit.SHORT_SIDE
    assert half_board_split("MDP", 36.0) is HalfBoardSplit.NONE


def test_an_unknown_or_missing_subtype_keeps_todays_behavior():
    """A board registered by hand, or a vendor ``TIPO`` this build doesn't know
    yet, must not lose the half board it is quoted with today."""
    assert half_board_split(None, 15.0) is HalfBoardSplit.LONG_SIDE
    assert half_board_split("TABLERO NUEVO", 15.0) is HalfBoardSplit.LONG_SIDE
    assert half_board_split(None, None) is HalfBoardSplit.LONG_SIDE


# --- the policy becomes geometry ------------------------------------------------
def test_long_side_halves_the_width():
    """Parallel to the largo: the sheet keeps its length. What every board did."""
    spec = OptimizationService._half_spec(_material(HalfBoardSplit.LONG_SIDE), MARKUP)
    assert (spec.width, spec.height) == (WIDTH / 2, HEIGHT)
    assert spec.half_board is True
    assert spec.key == "b1"


def test_short_side_halves_the_height():
    """Parallel to the lado corto: plywood and MDF ranurado."""
    spec = OptimizationService._half_spec(_material(HalfBoardSplit.SHORT_SIDE), MARKUP)
    assert (spec.width, spec.height) == (WIDTH, HEIGHT / 2)
    assert spec.half_board is True


def test_a_material_that_is_never_halved_has_no_half_bin():
    """``None`` is what switches the half board off: the whole pipeline already
    accepts it (``pool``/``parallel`` treat it as "this material has one bin")."""
    assert (
        OptimizationService._half_spec(_material(HalfBoardSplit.NONE), MARKUP) is None
    )


def test_the_price_does_not_depend_on_the_axis():
    """Half the sheet is half the sheet, whichever way the saw ran."""
    expected = round(COST / 2 * (1 + MARKUP), 2)
    for split in (HalfBoardSplit.LONG_SIDE, HalfBoardSplit.SHORT_SIDE):
        spec = OptimizationService._half_spec(_material(split), MARKUP)
        assert spec.cost_per_unit == expected
        assert spec.area == WIDTH * HEIGHT / 2


def test_an_inline_material_never_gets_a_half():
    """An offcut is a physical piece somebody owns; there is no half of it to sell."""
    material = _material(
        HalfBoardSplit.LONG_SIDE, source=MaterialSource.client_offcut.value
    )
    assert OptimizationService._half_spec(material, MARKUP) is None
