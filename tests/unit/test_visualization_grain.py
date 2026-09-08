"""The wood grain runs along the board's LARGO, and nothing else does.

The one claim worth pinning is the axis. Everything in this system enters the
largo first (``Requirement.height``, ``BoardAttributes.height``), and the diagram
draws the board rotated 90 degrees, so the largo lands on the canvas's horizontal
axis -- which makes the grain a set of horizontal lines. Swap the two dimensions
anywhere along that chain and the drawing stays plausible while being wrong, so
the test reads the pixels rather than the code.

No DB and no PDF: ``generate_layout_image`` is a pure function of a layout dict.
"""

import math

from PIL import Image

from src.modules.optimizations import visualization as viz

BOARD_WIDTH = 2070  # ancho
BOARD_HEIGHT = 2440  # largo
# Inset used when reading the board's pixels: the board outline is 3px of solid ink.
EDGE = 6
# Share of a row (or column) that has to be inked for it to count as a grain line.
INKED = 0.55


def _bare_board(width=BOARD_WIDTH, height=BOARD_HEIGHT):
    """A sheet with nothing on it: the grain is then the only mark inside it."""
    return {
        "pattern_id": 1,
        "count": 1,
        "layout": {
            "material": {"width": width, "height": height, "material_key": "b1"},
            "placed_pieces": [],
            "remainders": [],
            "cuts": [],
            "statistics": {"efficiency": 0.0},
        },
    }


def viz_scale(material):
    return 2000 / max(material["width"], material["height"])


def _is_grain(pixel):
    """Grain pixels are the only non-white ones on a bare board."""
    return pixel != (255, 255, 255)


def _board_pixels(group, mono=False):
    """Renders and returns a fast pixel accessor plus the board's pixel box."""
    buffer, _ = viz.VisualizationService.generate_layout_image(group, mono=mono)
    img = Image.open(buffer).convert("RGB")
    material = group["layout"]["material"]
    scale = viz_scale(material)
    return (
        img.load(),
        60,  # margin
        150,  # info_height
        int(material["height"] * scale),
        int(material["width"] * scale),
    )


def _inked_rows_and_cols(px, bx, by, board_w_px, board_h_px):
    """Rows and columns of the board that are mostly painted.

    A horizontal grain inks most of a ROW and only a fraction of any column; a
    vertical one does the exact opposite, so the asymmetry is the direction.

    A FRACTION and not "end to end": the lines are broken on purpose (see
    ``GRAIN_DASHES_MM``), so no row is ever fully painted. The duty cycle of every
    dash pattern is around 87% and a column only ever meets the lines it crosses,
    which is nearer 12%, so the threshold sits comfortably between the two.

    Inset past ``EDGE``: the board's own outline is 3px of solid ink and would
    otherwise count as a fully painted first row and column.
    """
    lo, hi_x, hi_y = EDGE, board_w_px - EDGE, board_h_px - EDGE
    xs = list(range(lo, hi_x, 4))
    ys = list(range(lo, hi_y, 4))
    rows = [
        dy
        for dy in range(lo, hi_y)
        if sum(_is_grain(px[bx + dx, by + dy]) for dx in xs) > len(xs) * INKED
    ]
    cols = [
        dx
        for dx in range(lo, hi_x)
        if sum(_is_grain(px[bx + dx, by + dy]) for dy in ys) > len(ys) * INKED
    ]
    return rows, cols


def test_grain_runs_along_the_largo():
    """Whole rows are painted and no whole column is. That asymmetry IS the
    direction, and it inverts exactly when the two axes are swapped."""
    group = _bare_board()
    px, bx, by, board_w_px, board_h_px = _board_pixels(group)

    rows, cols = _inked_rows_and_cols(px, bx, by, board_w_px, board_h_px)

    expected_lines = int(BOARD_WIDTH / viz.GRAIN_STEP_MM) - 1
    assert len(rows) >= expected_lines, "fewer grain lines than expected"
    assert cols == [], "the grain appears to run across the ancho"


def test_grain_is_a_texture_and_not_a_ruling():
    """Fine, closely spaced and IRREGULAR, the way wood looks. Evenly spaced
    continuous rules read as a grid drawn over the plan, which is what this
    replaced -- twice, since the first fix only made them denser."""
    assert viz.GRAIN_STEP_MM <= 30, "grain lines are too far apart to read as grain"
    assert viz.GRAIN_STROKE_MM < viz.GRAIN_STEP_MM / 6, "grain lines are too heavy"
    # Fixed millimetres, so a client's small offcut is still visibly grained.
    assert 200 / viz.GRAIN_STEP_MM >= 3

    # Every line is broken: an even-indexed run of ink, then a gap, and so on.
    for dashes in viz.GRAIN_DASHES_MM:
        assert len(dashes) % 2 == 0, "a dash pattern with no closing gap is solid"
        assert all(d > 0 for d in dashes), "a zero-length run makes the line solid"
        ink = sum(dashes[0::2])
        assert 0.7 < ink / sum(dashes) < 0.95, "the line reads as dotted or as solid"

    # ...and no two lines share a weight and a nudge for long: the three tables are
    # coprime, so the texture only repeats after 385 lines.
    assert math.gcd(len(viz.GRAIN_DASHES_MM), len(viz.GRAIN_WEIGHTS)) == 1
    assert math.gcd(len(viz.GRAIN_WEIGHTS), len(viz.GRAIN_JITTER)) == 1
    assert math.gcd(len(viz.GRAIN_DASHES_MM), len(viz.GRAIN_JITTER)) == 1
    assert max(abs(j) for j in viz.GRAIN_JITTER) < 0.5, "jitter can collide two lines"


def _distinct_colors(px, bx, by, x0, x1, y0, y1):
    """Colours seen inside a screen-space box of the board."""
    return {px[bx + dx, by + dy] for dy in range(y0, y1, 3) for dx in range(x0, x1, 3)}


def test_grain_covers_pieces_and_offcuts_alike():
    """The grain belongs to the sheet, so it is painted over whatever is on it --
    a piece the optimizer rotated does not get a direction of its own.

    Counted as distinct colours rather than as "not white": a piece and an offcut
    are filled, so every pixel inside them is already non-white and the direction
    test's predicate says nothing here. A flat fill shows ONE colour; a fill with
    the grain over it shows more.
    """
    group = _bare_board()
    group["layout"]["placed_pieces"] = [
        {
            "piece_id": "Lateral#1",
            "x": 0,
            "y": 0,
            "width": 900,
            "height": 1500,
            "original_width": 1500,
            "original_height": 900,
            "rotated": True,
            "edges": {},
        }
    ]
    group["layout"]["remainders"] = [{"x": 1000, "y": 0, "width": 900, "height": 1500}]

    px, bx, by, _, _ = _board_pixels(group)

    # Screen-space boxes, well inside each region (see ``boardRotation``: a board
    # point (x, y) lands at (H - y, x)).
    regions = {
        "piece": (800, 1900, 50, 700),
        "offcut": (800, 1900, 900, 1500),
        "bare sheet": (100, 700, 100, 1600),
    }
    for name, (x0, x1, y0, y1) in regions.items():
        colors = _distinct_colors(px, bx, by, x0, x1, y0, y1)
        assert len(colors) > 1, f"the grain does not run over the {name}"


def test_grain_is_drawn_in_both_themes():
    """The production sheet is monochrome and prints on the shop's own printer;
    an overlay that only exists in the branded theme would miss the reader who
    needs it most."""
    for mono in (False, True):
        px, bx, by, board_w_px, board_h_px = _board_pixels(_bare_board(), mono=mono)
        inside = [px[bx + board_w_px // 2, by + dy] for dy in range(2, board_h_px - 2)]
        assert any(_is_grain(p) for p in inside), f"no grain with mono={mono}"
