"""Tests for the pure cutting domain (no frameworks)."""

import pytest

from src.cutting import (
    BinSpec,
    CuttingLayout,
    CuttingParameters,
    GuillotineOptimizer,
    Material,
    MultiSheetGuillotineOptimizer,
    Piece,
    Rectangle,
    Selection,
    SplitRule,
)
from src.cutting.constructors import GreedyConfig, greedy_fill

# --- Models ----------------------------------------------------------------


def test_rectangle_area_and_contains():
    rect = Rectangle(x=0, y=0, width=100, height=50)
    assert rect.area == 5000
    assert rect.contains(80, 40) is True
    assert rect.contains(120, 40) is False


def test_rectangle_negative_dimensions_raise():
    with pytest.raises(ValueError):
        Rectangle(x=0, y=0, width=-1, height=10)


def test_piece_validations():
    piece = Piece(id="p1", width=300, height=200, quantity=2)
    assert piece.area == 60000
    with pytest.raises(ValueError):
        Piece(id="bad", width=0, height=10)
    with pytest.raises(ValueError):
        Piece(id="bad", width=10, height=10, quantity=0)


def test_material_validations():
    material = Material(id="m1", width=1220, height=2440, thickness=18)
    assert material.area == 1220 * 2440
    with pytest.raises(ValueError):
        Material(id="bad", width=0, height=10, thickness=18)
    with pytest.raises(ValueError):
        Material(id="bad", width=10, height=10, thickness=-1)


def test_cutting_parameters_reject_negative():
    params = CuttingParameters(kerf=5, top_trim=1)
    assert params.kerf == 5
    with pytest.raises(ValueError):
        CuttingParameters(kerf=-1)
    with pytest.raises(ValueError):
        CuttingParameters(left_trim=-1)


# --- Single-sheet optimizer -------------------------------------------------


def test_guillotine_places_pieces_and_reports_efficiency():
    material = Material(id="m1", width=1220, height=2440, thickness=18)
    optimizer = GuillotineOptimizer(
        material=material, cutting_params=CuttingParameters(kerf=4)
    )
    placed, unplaced = optimizer.optimize([Piece(id="a", width=600, height=400)])

    assert len(placed) == 1
    assert unplaced == []

    layout = CuttingLayout(
        material=material,
        placed_pieces=placed,
        remainders=optimizer.remainders,
        sheet_number=1,
    )
    assert layout.used_area == 600 * 400
    assert layout.waste_area == material.area - layout.used_area
    assert 0 < layout.efficiency < 1

    as_dict = layout.to_dict()
    assert as_dict["statistics"]["pieces_count"] == 1
    assert as_dict["material"]["material_key"] == "m1"
    assert as_dict["material"]["sheet_number"] == 1
    # Saw cuts are serialized (consumed by the workshop view).
    assert as_dict["cuts"] == [
        {"x": c.x, "y": c.y, "length": c.length, "is_horizontal": c.is_horizontal}
        for c in layout.cuts
    ]


def test_guillotine_empty_pieces_returns_empty():
    material = Material(id="m1", width=1000, height=1000, thickness=18)
    placed, unplaced = GuillotineOptimizer(material=material).optimize([])
    assert placed == []
    assert unplaced == []


# --- Cut linear meters (exact reconstruction) -------------------------------


def test_cut_length_single_piece_vertical_first():
    """SHORTER_LEFTOVER_AXIS with a smaller width leftover -> ``vertical_first``.

    400(width)x300(height) piece on a 1000x1000 board (kerf 0): a horizontal cut
    at the full width (1000) + a vertical cut at the piece's height (300) = 1300 mm.
    """
    material = Material(id="m1", width=1000, height=1000, thickness=18)
    optimizer = GuillotineOptimizer(
        material=material, cutting_params=CuttingParameters(kerf=0)
    )
    placed, unplaced = optimizer.optimize(
        [Piece(id="p", width=400, height=300, can_rotate=False)]
    )
    assert len(placed) == 1 and unplaced == []

    horizontals = [c for c in optimizer.cuts if c.is_horizontal]
    verticals = [c for c in optimizer.cuts if not c.is_horizontal]
    assert len(horizontals) == 1 and horizontals[0].length == pytest.approx(1000)
    assert len(verticals) == 1 and verticals[0].length == pytest.approx(300)

    layout = CuttingLayout(
        material=material,
        placed_pieces=placed,
        remainders=optimizer.remainders,
        sheet_number=1,
        cuts=optimizer.cuts,
    )
    assert layout.cut_length == pytest.approx(1300)


def test_cut_length_single_piece_horizontal_first():
    """A larger width leftover -> ``horizontal_first``.

    300(width)x600(height) piece on a 1000x1000 board (kerf 0): a vertical cut at
    the full height (1000) + a horizontal cut at the piece's width (300) = 1300 mm.
    """
    material = Material(id="m1", width=1000, height=1000, thickness=18)
    optimizer = GuillotineOptimizer(
        material=material, cutting_params=CuttingParameters(kerf=0)
    )
    placed, unplaced = optimizer.optimize(
        [Piece(id="p", width=300, height=600, can_rotate=False)]
    )
    assert len(placed) == 1 and unplaced == []

    horizontals = [c for c in optimizer.cuts if c.is_horizontal]
    verticals = [c for c in optimizer.cuts if not c.is_horizontal]
    assert len(verticals) == 1 and verticals[0].length == pytest.approx(1000)
    assert len(horizontals) == 1 and horizontals[0].length == pytest.approx(300)


def test_cut_length_full_fill_has_no_cuts():
    """A piece that exactly fills the board generates no cuts (no leftover)."""
    material = Material(id="m1", width=500, height=500, thickness=18)
    optimizer = GuillotineOptimizer(
        material=material, cutting_params=CuttingParameters(kerf=0)
    )
    placed, unplaced = optimizer.optimize(
        [Piece(id="p", width=500, height=500, can_rotate=False)]
    )
    assert len(placed) == 1 and unplaced == []
    assert optimizer.cuts == []


def test_guillotine_trims_exceeding_material_raise():
    material = Material(id="m1", width=100, height=100, thickness=18)
    with pytest.raises(ValueError):
        GuillotineOptimizer(
            material=material,
            cutting_params=CuttingParameters(left_trim=60, right_trim=60),
        )


# --- Multi-sheet optimizer ---------------------------------------------------


def test_multisheet_uses_several_sheets_when_needed():
    material = Material(id="board", width=1000, height=1000, thickness=18)
    optimizer = MultiSheetGuillotineOptimizer(
        material_template=material,
        cutting_params=CuttingParameters(kerf=5),
        split_rule=SplitRule.SHORTER_LEFTOVER_AXIS,
        max_sheets=10,
    )
    # 6 large pieces that don't all fit on a single sheet.
    layouts, remaining = optimizer.optimize(
        [Piece(id="big", width=700, height=700, quantity=6)]
    )

    assert len(layouts) >= 2
    assert remaining == []
    assert all(isinstance(layout, CuttingLayout) for layout in layouts)
    # The material keeps the original board_id; the sheet number lives on the layout.
    assert layouts[0].material.id == "board"
    assert [layout.sheet_number for layout in layouts] == list(
        range(1, len(layouts) + 1)
    )


def test_multisheet_empty_returns_empty():
    material = Material(id="board", width=1000, height=1000, thickness=18)
    layouts, remaining = MultiSheetGuillotineOptimizer(
        material_template=material
    ).optimize([])
    assert layouts == []
    assert remaining == []


def test_multisheet_layouts_track_cut_length():
    """Each layout reports its cut length (sum of its segments) and it is positive."""
    material = Material(id="board", width=1000, height=1000, thickness=18)
    optimizer = MultiSheetGuillotineOptimizer(
        material_template=material,
        cutting_params=CuttingParameters(kerf=5),
    )
    layouts, remaining = optimizer.optimize(
        [Piece(id="p", width=400, height=300, quantity=3, can_rotate=False)]
    )
    assert layouts and remaining == []
    for layout in layouts:
        assert layout.cut_length == pytest.approx(sum(c.length for c in layout.cuts))
        assert layout.cut_length > 0


# --- Free-rect selection (Selection) -----------------------------------------
#
# ``Selection`` is no longer a user-facing heuristic: it is one axis of the
# search's candidate portfolio (``constructors.GREEDY_PORTFOLIO``), so it is
# exercised where it lives -- on a single greedy fill -- rather than through a
# strategy argument the API no longer has.


def _example_setup():
    """Portrait board (height 2440 > width 1830) + 8 pieces from the real example."""
    material = Material(id="board", width=1830, height=2440, thickness=15)
    params = CuttingParameters(
        kerf=5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
    )
    widths = [300, 500, 302, 303, 304, 305, 306, 307]
    pieces = [
        Piece(id=f"P{i + 1}", width=w, height=500, quantity=1, can_rotate=False)
        for i, w in enumerate(widths)
    ]
    return material, params, pieces


def _example_spec(material):
    return BinSpec(
        key=material.id,
        width=material.width,
        height=material.height,
        thickness=material.thickness,
        cost_per_unit=100.0,
    )


# The two portfolio points that bracket the axis: Best-Area-Fit with the default
# split (what a direct caller gets) and Bottom-Left with ``LONGER_AXIS``, the
# combination that concentrates the waste into one strip.
_BAF = GreedyConfig(
    sort="area",
    split=SplitRule.SHORTER_LEFTOVER_AXIS,
    selection=Selection.BEST_AREA_FIT,
)
_BOTTOM_LEFT = GreedyConfig(
    sort="height",
    split=SplitRule.LONGER_AXIS,
    selection=Selection.BOTTOM_LEFT,
)


def test_split_rule_defaults_to_shorter_leftover_axis():
    """Without an explicit ``split_rule`` both optimizers take the default one."""
    material = Material(id="m", width=1830, height=2440, thickness=15)
    assert (
        MultiSheetGuillotineOptimizer(material_template=material).split_rule
        == SplitRule.SHORTER_LEFTOVER_AXIS
    )
    assert (
        GuillotineOptimizer(material=material).split_rule
        == SplitRule.SHORTER_LEFTOVER_AXIS
    )
    # An explicit ``split_rule`` wins over the default.
    override = GuillotineOptimizer(
        material=material,
        split_rule=SplitRule.MAXIMIZE_AREA,
        selection=Selection.BOTTOM_LEFT,
    )
    assert override.split_rule == SplitRule.MAXIMIZE_AREA


def test_default_search_preserves_best_area_fit_layout():
    """The engine's default preserves the historical behavior (BAF)."""
    material, params, pieces = _example_setup()
    optimizer = MultiSheetGuillotineOptimizer(
        material_template=material, cutting_params=params
    )
    layouts, remaining = optimizer.optimize(pieces)

    assert remaining == []
    layout = layouts[0]
    biggest = max(layout.remainders, key=lambda r: r.area)
    # Largest leftover from the real example with BAF: 1502 (width) x 1915 (height).
    assert biggest.width == pytest.approx(1502)
    assert biggest.height == pytest.approx(1915)
    # BAF spreads pieces across the width (they reach past the right half).
    assert max(p.x + p.width for p in layout.placed_pieces) > 1700


def test_bottom_left_leaves_full_height_strip_against_one_side():
    """``BOTTOM_LEFT`` pushes pieces to the left and leaves a full-height strip."""
    material, params, pieces = _example_setup()
    fill = greedy_fill(pieces, _example_spec(material), params, _BOTTOM_LEFT)

    assert fill is not None and len(fill.placed) == len(pieces)
    usable_height = material.height - params.top_trim - params.bottom_trim  # 2420

    # The dominant leftover is a strip spanning the full usable height.
    biggest = max(fill.remainders, key=lambda r: r.area)
    assert biggest.height == pytest.approx(usable_height)
    # It hugs one side: pieces don't encroach on it (all to its left).
    assert all(p.x + p.width <= biggest.x + 1e-6 for p in fill.placed)
    # Pieces end up packed against the left side of the board.
    assert max(p.x + p.width for p in fill.placed) < material.width / 2


def test_bottom_left_differs_from_best_area_fit():
    """The two selections produce different layouts for the same input."""
    material, params, pieces = _example_setup()
    spec = _example_spec(material)
    baf = greedy_fill(pieces, spec, params, _BAF)
    bottom_left = greedy_fill(pieces, spec, params, _BOTTOM_LEFT)

    assert baf is not None and bottom_left is not None
    baf_xs = sorted((p.piece.id, p.x, p.y) for p in baf.placed)
    bl_xs = sorted((p.piece.id, p.x, p.y) for p in bottom_left.placed)
    assert baf_xs != bl_xs
