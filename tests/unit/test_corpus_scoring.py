"""Unit: what a commercial board count is evidence of.

``corpus_labels`` reads the number off a filename; ``corpus_scoring`` decides
whether that number graded our packing or recorded a sale. Getting this wrong
does not fail loudly -- it reports a deficit against a bound nobody reaches, and
sends the next month of engine work at a target that cannot move.

Every case below is a real pool from the shop's 2026 exports, with its
dimensions **inlined**: the corpus is gitignored (the exports carry client
names), so a test that opened the files would pass locally and skip in CI.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from corpus_scoring import (  # noqa: E402
    FLOOR,
    PLAN,
    area_floor,
    capacity,
    classify,
    half_usable_area,
    implied_efficiency,
    raw_area,
    usable_area,
)

from src.cutting import CuttingParameters  # noqa: E402

# The shop's real saw and dressing, confirmed with the operator.
SHOP = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)

BLANCO = {"width": 2070.0, "height": 2440.0}  # código 155
PLYWOOD = {"width": 1220.0, "height": 2440.0}


def test_the_trims_are_not_free():
    """1.8% of every sheet is dressed off before a part can go on it."""
    assert raw_area(BLANCO) == pytest.approx(5_050_800.0)
    assert usable_area(BLANCO, SHOP) == pytest.approx(2050 * 2420)
    assert usable_area(BLANCO, SHOP) < raw_area(BLANCO) * 0.983


def test_a_half_board_loses_a_second_pair_of_trims():
    """The rip exposes two fresh edges, so a half is not half a usable board."""
    assert half_usable_area(BLANCO, SHOP) == pytest.approx(1015 * 2420)
    assert half_usable_area(BLANCO, SHOP) < usable_area(BLANCO, SHOP) / 2


def test_capacity_counts_fulls_plus_at_most_one_half():
    room = capacity(2.5, BLANCO, SHOP)
    assert room == pytest.approx(
        2 * usable_area(BLANCO, SHOP) + half_usable_area(BLANCO, SHOP)
    )


def test_the_floor_divides_by_the_nominal_sheet():
    """The billing hypothesis under test: a seller knows nothing about the saw.

    Deliberately *not* the usable area -- see the module docstring in
    ``corpus_scoring``. Making the two denominators agree would erase the
    distinction the whole split rests on.
    """
    assert area_floor(raw_area(BLANCO) * 1.01, BLANCO) == 1.5
    assert area_floor(raw_area(BLANCO) * 0.99, BLANCO) == 1.0


def test_a_job_never_floors_below_a_half_board():
    """One small part still consumes something the shop has to charge for."""
    assert area_floor(200 * 300, BLANCO) == 0.5


def test_one_part_on_a_whole_sheet_billed_medio_is_a_sale_not_a_plan():
    """``MOISES MOLINA-MEDIO PLYWOOD8MM``: one 900x760 part, filed as MEDIO.

    23% of the board. No optimizer decided that; a seller charged half a sheet
    because more than half was left over. It is the cleanest case in the corpus
    of a label that grades nothing.
    """
    parts = 900 * 760
    assert area_floor(parts, PLYWOOD) == 0.5
    assert classify(0.5, parts, PLYWOOD) == FLOOR


def test_a_label_above_the_area_bound_is_a_cutting_decision():
    """Two boards' worth of parts billed as three: they opened one the area
    did not demand, which is the same thing our engine has to do."""
    parts = raw_area(BLANCO) * 2.1
    assert area_floor(parts, BLANCO) == 2.5
    assert classify(3.0, parts, BLANCO) == PLAN


def test_a_label_sitting_exactly_on_the_bound_grades_nothing():
    parts = raw_area(BLANCO) * 2.1
    assert classify(2.5, parts, BLANCO) == FLOOR


def test_implied_efficiency_exposes_an_uncuttable_label():
    """Eleven pools in the 2026 corpus need more than the whole usable board.

    They survived the old gate because it measured against the nominal sheet,
    and they scored as 5.5 boards of deficit.
    """
    used = usable_area(BLANCO, SHOP) * 1.02
    assert implied_efficiency(used, 1.0, BLANCO, SHOP) > 1.0


def test_implied_efficiency_is_measured_against_the_usable_board():
    used = usable_area(BLANCO, SHOP) * 0.8
    assert implied_efficiency(used, 1.0, BLANCO, SHOP) == pytest.approx(0.8)
