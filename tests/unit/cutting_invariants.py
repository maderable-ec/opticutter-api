"""Physical validity checks shared by the cutting-engine unit tests.

Every layout the engine emits — whichever constructor, search stage or solver
produced it — has to survive the same reality check: pieces inside the trimmed
area, never overlapping, always at least a blade's width apart, rotated only
when the piece allows it, and each instance placed exactly once.

Keeping this in one place is deliberate: a new candidate generator is only
trustworthy once it passes the *same* assertions as the old ones.
"""

from src.cutting import CuttingLayout


def assert_valid_layouts(layouts, unplaced, params, expected_instances):
    """Bounds, trims, kerf separation, rotation legality and piece conservation."""
    placed_ids = []
    for layout in layouts:
        mat = layout.material
        usable_x0 = params.left_trim
        usable_y0 = params.bottom_trim
        usable_x1 = mat.width - params.right_trim
        usable_y1 = mat.height - params.top_trim
        pieces = layout.placed_pieces
        for pp in pieces:
            placed_ids.append(pp.piece.id)
            # Inside the usable area (trims respected).
            assert pp.x >= usable_x0 - 1e-6 and pp.y >= usable_y0 - 1e-6
            assert pp.x + pp.width <= usable_x1 + 1e-6
            assert pp.y + pp.height <= usable_y1 + 1e-6
            # Rotation only when allowed; dims consistent with the original.
            if pp.rotated:
                assert pp.piece.can_rotate
                assert (pp.width, pp.height) == (pp.piece.height, pp.piece.width)
            else:
                assert (pp.width, pp.height) == (pp.piece.width, pp.piece.height)
        # Pairwise: no overlap, and any separation leaves room for the blade.
        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                a, b = pieces[i], pieces[j]
                gap_x = max(b.x - (a.x + a.width), a.x - (b.x + b.width))
                gap_y = max(b.y - (a.y + a.height), a.y - (b.y + b.height))
                # Separated on at least one axis...
                assert max(gap_x, gap_y) >= -1e-6, f"overlap: {a} vs {b}"
                # ...and by at least the kerf on the separating axis.
                assert (
                    max(gap_x, gap_y) >= params.kerf - 1e-6
                ), f"kerf violated between {a.piece.id} and {b.piece.id}"
        # Leftovers are real rectangles, never negative slivers.
        for rect in layout.remainders:
            assert rect.width >= 0 and rect.height >= 0
    # Every instance placed exactly once (or explicitly unplaced).
    assert len(placed_ids) == len(set(placed_ids))
    assert len(placed_ids) + len(unplaced) == expected_instances


def assert_valid_fill(fill, params, expected_instances):
    """Same checks for a single-bin ``BinFill`` (what the constructors emit)."""
    layout = CuttingLayout(
        material=fill.spec.to_material(),
        placed_pieces=fill.placed,
        remainders=fill.remainders,
        cuts=fill.cuts,
    )
    assert_valid_layouts([layout], [], params, expected_instances)
