"""Unit: whole boards before the half board, and the grouping that makes it hold.

The engine treats a half board as a swap of a whole one (``half_downgrade``
replaces the fill in place), so a half keeps whatever position the board it
replaced happened to hold and shows up interleaved in the diagram. The shop
cuts the whole boards first and the half last, so the payload is ordered that
way — once, in the backend, since the optimizer view, the client review, the
production PDF and the workshop board all read the same list.
"""

import pytest

from src.cutting import BinSpec, CuttingParameters, Piece
from src.cutting.models import CuttingLayout, Material
from src.cutting.search import optimize_bins
from src.modules.optimizations.patterns import (
    group_layouts,
    layout_signature,
    order_sheets,
)


def _layout(key="b1", half=False, sheet_number=1, x=10.0):
    width = 610.0 if half else 1220.0
    return {
        "material": {
            "material_key": key,
            "sheet_number": sheet_number,
            "width": width,
            "height": 2440.0,
            "thickness": 15.0,
            "area": width * 2440.0,
            "half_board": half,
        },
        "placed_pieces": [
            {
                "piece_id": "p1",
                "x": x,
                "y": 10.0,
                "width": 300.0,
                "height": 600.0,
                "rotated": False,
            }
        ],
        "statistics": {"efficiency": 50.0, "pieces_count": 1},
        "remainders": [],
        "cuts": [],
    }


def _sheet(key="b1", half=False, sheet_number=1):
    """A ``CuttingLayout``, the shape ``_build_result_payload`` orders."""
    width = 610.0 if half else 1220.0
    return CuttingLayout(
        material=Material(
            id=key,
            width=width,
            height=2440.0,
            thickness=15.0,
            cost_per_unit=25.0 if half else 45.5,
            half_board=half,
        ),
        placed_pieces=[],
        remainders=[],
        sheet_number=sheet_number,
    )


class TestSignature:
    def test_a_half_and_a_whole_board_are_different_patterns(self):
        """Their placements match — the half's content fits either — so without
        ``half_board`` in the signature they merge, the diagram draws one board
        size for both, and the half disappears into the whole board's group."""
        whole = _layout(half=False)
        half = _layout(half=True)
        assert layout_signature(whole) != layout_signature(half)

        groups = group_layouts([whole, half])
        assert len(groups) == 2
        assert [g["layout"]["material"]["half_board"] for g in groups] == [False, True]

    def test_two_whole_boards_with_the_same_cuts_still_share_a_pattern(self):
        """The deduplication the signature exists for is untouched."""
        groups = group_layouts([_layout(sheet_number=1), _layout(sheet_number=2)])
        assert len(groups) == 1
        assert groups[0]["count"] == 2


class TestOrdering:
    def test_the_half_board_goes_last(self):
        layouts = order_sheets(
            [
                _layout(sheet_number=1),
                _layout(half=True, sheet_number=2),
                _layout(sheet_number=3),
            ]
        )
        assert [x["material"]["half_board"] for x in layouts] == [False, False, True]

    def test_the_search_order_survives_inside_each_half(self):
        """A stable sort: whichever whole board the search put first stays first,
        which is what keeps the plan reproducible."""
        layouts = order_sheets([_layout(sheet_number=n) for n in (3, 1, 2)])
        assert [x["material"]["sheet_number"] for x in layouts] == [3, 1, 2]

    def test_layout_groups_inherit_the_order(self):
        """The diagram reads ``layout_groups``, not ``layouts``."""
        layouts = order_sheets(
            [_layout(half=True, sheet_number=1), _layout(sheet_number=2)]
        )
        groups = group_layouts(layouts)
        assert [g["layout"]["material"]["half_board"] for g in groups] == [False, True]

    def test_sorting_per_pool_never_interleaves_two_materials(self):
        """The sort runs inside the per-material loop, so a job with two boards
        keeps them in blocks — b1's half must not jump past b2's whole boards."""
        pools = [
            [
                _layout(key="b1", sheet_number=1),
                _layout(key="b1", half=True, sheet_number=2),
            ],
            [_layout(key="b2", sheet_number=1, x=20.0)],
        ]
        flat = [x for pool in pools for x in order_sheets(pool)]
        assert [x["material"]["material_key"] for x in flat] == ["b1", "b1", "b2"]
        assert [x["material"]["half_board"] for x in flat] == [False, True, False]


# A pool where the engine genuinely emits the half board FIRST — the shape this
# whole change exists for. Found by sweeping randomized furniture-ish jobs; kept
# verbatim (ids included) because the id order is part of what the search's
# tiebreaks see, so paraphrasing it would stop reproducing the interleave.
_INTERLEAVING_POOL = [
    ("p0", 400.0, 800.0),
    ("p1", 900.0, 400.0),
    ("p2", 400.0, 1100.0),
    ("p3", 590.0, 600.0),
    ("p4", 900.0, 300.0),
    ("p5", 300.0, 600.0),
    ("p6", 590.0, 600.0),
    ("p7", 400.0, 1500.0),
    ("p8", 700.0, 600.0),
    ("p9", 590.0, 1500.0),
    ("p10", 500.0, 600.0),
    ("p11", 300.0, 400.0),
    ("p12", 300.0, 400.0),
    ("p13", 590.0, 400.0),
    ("p14", 500.0, 400.0),
    ("p15", 590.0, 1100.0),
]


@pytest.mark.slow
def test_the_engine_really_does_put_a_half_board_first():
    """Guards the premise, not just the fix.

    ``half_downgrade`` swaps a fill in place, so the half inherits the position
    of the whole board it replaced — on this pool that is sheet 1, and the
    diagram showed the medio tablero before the two whole boards. If a future
    engine change made this pool come back already ordered, the fix would still
    be right but this pool would stop testing anything, and the next person
    should know.
    """
    params = CuttingParameters(
        kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
    )
    bins = [
        BinSpec(
            key="b1", width=1220.0, height=2440.0, thickness=18, cost_per_unit=45.5
        ),
        BinSpec(
            key="b1",
            width=610.0,
            height=2440.0,
            thickness=18,
            cost_per_unit=25.03,
            half_board=True,
        ),
    ]
    pieces = [
        Piece(id=pid, width=w, height=h, can_rotate=True)
        for pid, w, h in _INTERLEAVING_POOL
    ]

    layouts, unplaced = optimize_bins(pieces, bins, params)
    assert unplaced == []

    raw = [x.material.half_board for x in layouts]
    assert raw != sorted(raw), "pool no longer interleaves; find another one"
    assert [x.material.half_board for x in order_sheets(layouts)] == sorted(raw)
