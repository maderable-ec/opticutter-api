"""Design families: the name key, and the coverage report that replaced two
catalog-sync warnings.

Those two warnings ("this family has no counterpart", "no stocked width covers
this board") used to be computed over the vendor's rows in
``catalog_sync._collect_warnings``. They moved when coordination stopped living
in the vendor's ``obs``: judged from source rows they would flag families we had
already fixed by hand and stay silent about the ones we break. The rules
themselves did not change, so these are the same cases, re-pointed at our data.
"""

from types import SimpleNamespace

import pytest

from src.modules.products.family_service import compute_family_stats
from src.modules.products.service import normalize_family
from src.shared.exceptions import ValidationError


def _board(thickness=15.0, active=True):
    return SimpleNamespace(
        type="board",
        is_active=active,
        alias=None,
        attributes={"thickness": thickness},
    )


def _band(width=22.0, alias="CSH", active=True):
    return SimpleNamespace(
        type="edge_banding",
        is_active=active,
        alias=alias,
        attributes={"width": width, "thickness": 0.45},
    )


def _stats(*members):
    return compute_family_stats({1: list(members)})[1]


# --- The matching key ---------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Cashmere", "cashmere"),
        ("  cashmere ", "cashmere"),
        ("OLMO PANELA", "olmo panela"),
        (None, ""),
        ("   ", ""),
    ],
)
def test_normalize_family_is_the_one_definition_of_same_family(value, expected):
    """Stored as ``product_families.normalized_name`` rather than enforced by a
    functional index on ``lower(name)``: ``casefold()`` is not Postgres'
    ``lower()``, and two definitions would diverge outside ASCII."""
    assert normalize_family(value) == expected


def test_a_blank_family_name_is_rejected():
    """It used to be possible to have a family that matched nothing: an empty
    ``family`` string in the bag was falsy, so the picker returned []. A name
    that normalizes to nothing cannot become a row."""
    from src.modules.products.family_service import ProductFamilyService

    with pytest.raises(ValidationError):
        ProductFamilyService._key("   ")


# --- Coverage -----------------------------------------------------------------


def test_a_coordinated_family_reports_nothing():
    st = _stats(_board(15.0), _band(19.0), _band(22.0))
    assert st.board_count == 1
    assert st.edge_banding_count == 2
    assert not st.has_no_boards
    assert not st.has_no_edge_bandings
    assert st.uncovered_thicknesses == []


def test_a_family_with_only_tapes_is_flagged():
    """18 of the catalog's 75 families are in this state: a tape nobody uses."""
    st = _stats(_band(22.0))
    assert st.has_no_boards
    assert not st.has_no_edge_bandings


def test_a_family_with_only_boards_is_flagged():
    """3 families. Their picker comes back empty, silently, for every board."""
    st = _stats(_board(15.0))
    assert st.has_no_edge_bandings
    assert not st.has_no_boards


def test_a_thickness_no_stocked_width_covers_is_reported():
    """The real case: a 36mm board whose design only comes in 19/22mm tape. From
    the seller's chair it is the same empty picker as a broken family."""
    st = _stats(_board(36.0), _band(19.0), _band(22.0))
    assert st.uncovered_thicknesses == [36.0]


def test_coverage_is_per_thickness_not_per_family():
    """A design can coordinate perfectly at 15mm and have nothing wide enough
    for its 36mm sibling. That is one gap, not a broken family."""
    st = _stats(_board(15.0), _board(36.0), _band(19.0))
    assert st.uncovered_thicknesses == [36.0]


def test_a_family_with_no_tapes_is_not_also_reported_as_a_width_gap():
    """One family, one reason. ``has_no_edge_bandings`` already says it, and a
    diagnostic list that says things twice stops being read."""
    st = _stats(_board(15.0), _board(36.0))
    assert st.has_no_edge_bandings
    assert st.uncovered_thicknesses == []


def test_inactive_products_do_not_count():
    """An inactive product coordinates with nothing (the picker filters it out),
    so counting it would paint a family as healthy while its picker is empty."""
    st = _stats(_board(15.0), _band(19.0, active=False))
    assert st.edge_banding_count == 0
    assert st.has_no_edge_bandings


def test_a_divergent_alias_is_visible():
    """Zero cases in the catalog today, and worth keeping at zero: two codes for
    one design means the thermal label stops telling the canteador anything."""
    st = _stats(_board(15.0), _band(19.0, alias="CSH"), _band(22.0, alias="CS2"))
    assert st.aliases == ["CS2", "CSH"]


def test_a_tape_without_an_alias_is_counted():
    st = _stats(_board(15.0), _band(19.0, alias=None), _band(22.0, alias="CSH"))
    assert st.missing_alias_count == 1
    assert st.aliases == ["CSH"]
