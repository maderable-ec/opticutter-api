"""Unit: the ``L`` of the edge notation is the pair of sides along the LONGER measure.

No DB. The shop's reading -- its files carry ``90×1200 1L CS`` for a strip
banded along its 1200 -- and the one the notation now speaks everywhere it is
computed. What is load-bearing, and silent when wrong:

- the stored sides stay physical (``left``/``right`` along the height), so a
  piece whose height is the longer measure reads exactly as it always did;
- on a piece whose width is longer the pairs swap letters, and so does the
  order the cantos especiales are written in (long sides first);
- only a quote with a row the two readings write differently moves in Redis,
  and only a plan with one is marked, which is what keeps an order frozen
  before the rule printing what the shop read.
"""

import pytest

from src.modules.optimizations.documents import _edge_banding_notation
from src.modules.optimizations.labels import (
    edge_banding_notation,
    edge_notation,
    length_changes_notation,
    long_sides,
    payload_reads_by_length,
)
from src.modules.optimizations.schemas import Requirement
from src.modules.optimizations.service import (
    OptimizationService,
    notation_reads_by_length,
)
from src.modules.orders.pieces_export import piece_etiqueta

from .test_pieces_export import _piece
from .test_special_edges import _TAPES


def _req(height, width, sides=None, special=()) -> Requirement:
    return Requirement(
        priority=0,
        height=height,
        width=width,
        quantity=1,
        material_key="b1",
        edge_banding={"sides": sides, "product_id": 7} if sides else None,
        special_edges=[{"side": s, "product_id": pid} for s, pid in special],
    )


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "height, width, expected",
    [
        (600, 300, ("left", "right")),  # the height is the longer measure
        (300, 600, ("top", "bottom")),  # the width is
        (500, 500, ("left", "right")),  # square: as it always read
        (None, None, ("left", "right")),  # unknown: the old reading
    ],
)
def test_the_long_sides_are_the_pair_along_the_longer_measure(height, width, expected):
    assert long_sides(height, width) == expected


@pytest.mark.parametrize(
    "sides, tall, wide",
    [
        (["left"], "1L CS", "1C CS"),
        (["top"], "1C CS", "1L CS"),
        (["left", "right"], "2L CS", "2C CS"),
        (["top", "left", "right"], "2L1C CS", "1L2C CS"),
        # Even on both pairs, the same text either way.
        (["top", "left"], "1L1C CS", "1L1C CS"),
        (["top", "bottom", "left", "right"], "4L CS", "4L CS"),
    ],
)
def test_the_letters_follow_the_longer_measure(sides, tall, wide):
    assert edge_banding_notation(sides, "Soft", height=600, width=300) == tall
    assert edge_banding_notation(sides, "Soft", height=300, width=600) == wide
    # Without measures, the reading by height: what every test before the rule
    # (and every unmarked payload) prints.
    assert edge_banding_notation(sides, "Soft") == tall


def test_a_strip_typed_2l_is_banded_along_its_length():
    """Order 290 in the shop's own data: a 70×2050 strip, ``2L``."""
    assert edge_banding_notation(["top", "bottom"], height=70, width=2050) == "2L"


def test_the_special_edges_are_written_long_sides_first():
    special = [
        {"side": "left", "band_type": "Hard", "alias": "BLN"},
        {"side": "top", "band_type": "Soft", "alias": "CHM"},
    ]
    assert edge_notation([], special=special, height=600, width=300) == (
        "1L CD BLN · 1C CS CHM"
    )
    # On a wider piece ``top`` is the long side, so its tape goes first.
    assert edge_notation([], special=special, height=300, width=600) == (
        "1L CS CHM · 1C CD BLN"
    )


def test_the_canto_and_a_special_edge_read_by_length_too():
    special = [{"side": "left", "band_type": "Hard", "alias": "BLN"}]
    assert edge_notation(["top"], "Soft", "CSH", special, height=300, width=600) == (
        "1L CS CSH · 1C CD BLN"
    )


# --------------------------------------------------------------------------- #
# What moves the hash and marks the payload
# --------------------------------------------------------------------------- #
def test_only_a_wider_piece_banded_unevenly_reads_otherwise():
    assert length_changes_notation(["left"], (), 300, 600)
    assert length_changes_notation(["top", "left", "right"], (), 300, 600)
    assert not length_changes_notation(["left"], (), 600, 300)
    assert not length_changes_notation(["left"], (), 500, 500)
    assert not length_changes_notation(["top", "left"], (), 300, 600)
    assert not length_changes_notation(["top", "bottom", "left", "right"], (), 300, 600)
    assert not length_changes_notation([], (), 300, 600)


def test_a_1l1c_split_between_two_tapes_reads_otherwise():
    """Even on both pairs overall, each tape is written on its own."""
    special = [{"side": "top", "alias": "9"}]
    assert length_changes_notation(["left"], special, 300, 600)


def test_the_rows_of_a_quote_decide_the_mark():
    assert not notation_reads_by_length([_req(600, 300, ["left"]), _req(300, 600)])
    assert notation_reads_by_length(
        [_req(600, 300, ["left"]), _req(300, 600, ["left"])]
    )
    assert notation_reads_by_length([_req(300, 600, ["left"], special=[("top", 9)])])
    assert not notation_reads_by_length([_req(300, 600, ["top", "left"])])


def test_the_mark_is_read_off_the_payload():
    assert payload_reads_by_length({"edge_notation": "by_length"})
    assert not payload_reads_by_length({})


# --------------------------------------------------------------------------- #
# Where the notation is computed and rebuilt
# --------------------------------------------------------------------------- #
def test_the_placed_piece_reads_its_own_measures(mock_session):
    svc = OptimizationService(mock_session)
    req = _req(300, 600, ["top"])
    straight = svc._geometric_edges(
        req.edge_banding, _TAPES, False, height=300, width=600
    )
    turned = svc._geometric_edges(req.edge_banding, _TAPES, True, height=300, width=600)
    assert straight["notation"] == turned["notation"] == "1L CS CSH"
    # The drawn side still turns with the piece; only the letter changed.
    assert (straight["sides"], turned["sides"]) == (["top"], ["right"])


def test_the_documents_table_follows_the_plans_reading():
    req = {"height": 300, "width": 600, "edge_banding": {"sides": ["left"]}}
    assert _edge_banding_notation(req, by_length=True) == "1C"
    # An order frozen before the rule keeps the text it was printed with.
    assert _edge_banding_notation(req) == "1L"


def test_the_export_follows_the_orders_reading():
    piece = _piece(height=300, width=600, edges={"sides": ["top"], "band_type": "Soft"})
    assert piece_etiqueta(piece, by_length=True) == "1L CS Puerta"
    assert piece_etiqueta(piece) == "1C CS Puerta"
