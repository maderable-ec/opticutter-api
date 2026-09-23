"""The physical checks a hand-edited sheet is held to (``src/cutting/sheet_check``)."""

import random

import pytest

from src.cutting import (
    BinSpec,
    CuttingParameters,
    Material,
    Piece,
    PlacedPiece,
    Rectangle,
)
from src.cutting.search import optimize_bins
from src.cutting.sheet_check import (
    INSIDE_TRIM,
    KERF,
    NOT_GUILLOTINE,
    OUT_OF_BOUNDS,
    OVERLAP,
    ROTATION_NOT_ALLOWED,
    UNVERIFIABLE,
    WHOLE_OFFCUT_OVERLAP,
    Placement,
    free_rectangles,
    leftover_extensions,
    placement_candidates,
    realize_sheet,
)

from .cutting_invariants import assert_valid_layouts

KERF_4 = CuttingParameters(kerf=4)
SHEET = Material(id="b", width=1000, height=1000, thickness=18)


def _at(piece, x, y, rotated=False):
    w, h = (piece.height, piece.width) if rotated else (piece.width, piece.height)
    return PlacedPiece(piece=piece, x=x, y=y, width=w, height=h, rotated=rotated)


def _codes(violations):
    return {v.code for v in violations}


# ---------------------------------------------------------------------------
# realize_sheet: the checks, then the tree
# ---------------------------------------------------------------------------


def test_a_piece_in_the_corner_derives_the_tree_consolidate_would():
    corner = Piece("P", 400, 300)
    layout, violations = realize_sheet(SHEET, [_at(corner, 0, 0)], KERF_4)

    assert violations == []
    # The full-width crosscut first: it keeps the one big offcut.
    assert [(c.x, c.y, c.length, c.is_horizontal) for c in layout.cuts] == [
        (0.0, 300.0, 1000.0, True),
        (400.0, 0.0, 300.0, False),
    ]
    assert {(r.x, r.y, r.width, r.height) for r in layout.remainders} == {
        (404.0, 0.0, 596.0, 300.0),
        (0.0, 304.0, 1000.0, 696.0),
    }


@pytest.mark.parametrize(
    "placed, code",
    [
        ([_at(Piece("A", 400, 300), 700, 0)], OUT_OF_BOUNDS),
        (
            [_at(Piece("A", 400, 300, can_rotate=False), 0, 0, rotated=True)],
            ROTATION_NOT_ALLOWED,
        ),
        (
            [_at(Piece("A", 400, 300), 0, 0), _at(Piece("B", 400, 300), 200, 100)],
            OVERLAP,
        ),
        ([_at(Piece("A", 400, 300), 0, 0), _at(Piece("B", 400, 300), 402, 0)], KERF),
    ],
)
def test_every_violation_is_named_with_its_pieces(placed, code):
    layout, violations = realize_sheet(SHEET, placed, KERF_4)

    assert layout is None
    assert code in _codes(violations)
    assert all(v.piece_ids for v in violations)


def test_the_trim_is_its_own_violation():
    params = CuttingParameters(kerf=4, left_trim=10, bottom_trim=10)
    _, violations = realize_sheet(SHEET, [_at(Piece("A", 400, 300), 5, 10)], params)

    assert _codes(violations) == {INSIDE_TRIM}


def test_touching_pieces_with_exactly_one_kerf_between_them_are_fine():
    a, b = Piece("A", 400, 300), Piece("B", 400, 300)
    layout, violations = realize_sheet(SHEET, [_at(a, 0, 0), _at(b, 404, 0)], KERF_4)

    assert violations == [] and layout is not None


def test_a_pinwheel_is_refused_as_not_guillotine():
    """Four pieces around a hole: kerf-clean, overlap-free, and no straight cut
    crosses the sheet without hitting one of them."""
    pieces = [
        _at(Piece("A", 600, 300), 0, 0),
        _at(Piece("B", 300, 600), 700, 0),
        _at(Piece("C", 600, 300), 400, 700),
        _at(Piece("D", 300, 600), 0, 400),
    ]

    layout, violations = realize_sheet(SHEET, pieces, KERF_4)

    assert layout is None
    assert _codes(violations) == {NOT_GUILLOTINE}


def test_running_out_of_nodes_is_not_a_verdict_but_it_is_refused():
    pieces = [
        _at(Piece(f"P{i}", 100, 100), (i % 5) * 150, (i // 5) * 150) for i in range(20)
    ]

    layout, violations = realize_sheet(SHEET, pieces, KERF_4, node_budget=3)

    assert layout is None
    assert _codes(violations) == {UNVERIFIABLE}


@pytest.mark.parametrize("seed", range(6))
def test_every_sheet_the_engine_emits_is_realizable(seed):
    """The checks never refuse what the search itself produces."""
    rng = random.Random(seed)
    pieces = [
        Piece(
            f"p{i}",
            rng.randint(80, 700),
            rng.randint(80, 700),
            can_rotate=rng.random() < 0.7,
        )
        for i in range(rng.randint(6, 22))
    ]
    params = CuttingParameters(
        kerf=4, left_trim=10, right_trim=10, top_trim=10, bottom_trim=10
    )
    layouts, unplaced = optimize_bins(
        pieces,
        [BinSpec(key="b", width=1220, height=2440, thickness=18, cost_per_unit=1)],
        params,
    )
    assert_valid_layouts(layouts, unplaced, params, len(pieces))

    for layout in layouts:
        realized, violations = realize_sheet(
            layout.material, layout.placed_pieces, params
        )
        assert violations == [], (seed, violations)
        assert realized.used_area == layout.used_area


# ---------------------------------------------------------------------------
# Whole offcuts
# ---------------------------------------------------------------------------


def test_a_whole_offcut_comes_back_in_one_piece_and_reshapes_the_tree():
    corner = _at(Piece("P", 400, 300), 0, 0)
    strip = Rectangle(404.0, 0.0, 596.0, 1000.0)

    layout, violations = realize_sheet(SHEET, [corner], KERF_4, [strip])

    assert violations == []
    # The rip goes first now, so the right-hand strip keeps the full height.
    assert (layout.cuts[0].x, layout.cuts[0].is_horizontal) == (400.0, False)
    assert (404.0, 0.0, 596.0, 1000.0) in {
        (r.x, r.y, r.width, r.height) for r in layout.remainders
    }
    assert layout.used_area == 400 * 300


def test_a_whole_offcut_is_still_room_for_a_piece_and_says_it_uses_it():
    """Growing an offcut makes room, and the seller then uses that room: the
    piece is offered on it, and the position names the whole offcut it uses."""
    corner = _at(Piece("P", 400, 300), 0, 0)
    strip = Rectangle(404.0, 0.0, 596.0, 1000.0)
    tall = Piece("Q", 500, 900, can_rotate=False)

    placements, free = placement_candidates(SHEET, [corner], tall, KERF_4, [strip])

    on_strip = [p for p in placements if (p.x, p.y) == (404.0, 0.0)]
    assert on_strip and on_strip[0].uses_whole_offcuts == (0,)
    assert (404, 0, 1000, 1000) in free
    layout, violations = realize_sheet(SHEET, [corner, _at(tall, 404, 0)], KERF_4)
    assert violations == [] and layout is not None


def test_a_position_clear_of_a_whole_offcut_keeps_it_whole():
    corner = _at(Piece("P", 400, 300), 0, 0)
    strip = Rectangle(404.0, 0.0, 596.0, 1000.0)

    placements, _ = placement_candidates(
        SHEET, [corner], Piece("S", 300, 200, can_rotate=False), KERF_4, [strip]
    )

    kept = [p for p in placements if p.x + 300 <= 400]
    assert kept and all(p.uses_whole_offcuts == () for p in kept)


def test_a_piece_may_not_cut_into_a_whole_offcut():
    layout, violations = realize_sheet(
        SHEET,
        [_at(Piece("P", 400, 300), 0, 0)],
        KERF_4,
        [Rectangle(402.0, 0.0, 598.0, 1000.0)],
    )

    assert layout is None
    assert _codes(violations) == {WHOLE_OFFCUT_OVERLAP}


# ---------------------------------------------------------------------------
# Free space and candidates
# ---------------------------------------------------------------------------


def test_free_rectangles_are_maximal_and_keep_a_blade_from_every_piece():
    free = free_rectangles((0, 0, 1000, 1000), [(0, 0, 400, 300)], 4)

    assert free == [(404, 0, 1000, 1000), (0, 304, 1000, 1000)]


def test_a_piece_that_only_fits_across_a_cut_is_still_offered():
    """The case the editor exists for: the derived tree crosscuts at y=300, and a
    tall piece fits on the right only if the rip is made first instead."""
    corner = _at(Piece("P", 400, 300), 0, 0)
    tall = Piece("Q", 500, 900, can_rotate=False)

    placements, _ = placement_candidates(SHEET, [corner], tall, KERF_4)

    assert (404.0, 0.0, False) in {(p.x, p.y, p.rotated) for p in placements}
    layout, violations = realize_sheet(SHEET, [corner, _at(tall, 404, 0)], KERF_4)
    assert violations == []
    assert (layout.cuts[0].x, layout.cuts[0].is_horizontal) == (400.0, False)


def test_a_grain_locked_piece_is_only_offered_upright():
    placements, _ = placement_candidates(
        SHEET, [], Piece("Q", 500, 200, can_rotate=False), KERF_4
    )

    assert placements and not any(p.rotated for p in placements)


def test_a_piece_that_fits_only_rotated_says_so():
    corner = _at(Piece("P", 1000, 700), 0, 0)
    placements, _ = placement_candidates(SHEET, [corner], Piece("Q", 200, 600), KERF_4)

    assert placements and all(p.rotated for p in placements)


@pytest.mark.parametrize("seed", range(8))
def test_every_candidate_offered_is_accepted(seed):
    """Whatever the editor is told it can do, the check lets it do."""
    rng = random.Random(seed)
    params = CuttingParameters(
        kerf=4, left_trim=10, right_trim=10, top_trim=10, bottom_trim=10
    )
    pieces = [
        Piece(f"p{i}", rng.randint(100, 600), rng.randint(100, 600)) for i in range(8)
    ]
    layouts, _ = optimize_bins(
        pieces,
        [BinSpec(key="b", width=1220, height=2440, thickness=18, cost_per_unit=1)],
        params,
    )
    sheet = layouts[0]
    moving = sheet.placed_pieces[rng.randrange(len(sheet.placed_pieces))]
    others = [pp for pp in sheet.placed_pieces if pp is not moving]

    # Half the seeds also keep their biggest offcut whole, so the offer is
    # checked with whole offcuts in play too.
    base, _ = realize_sheet(sheet.material, others, params)
    whole = [max(base.remainders, key=lambda r: r.area)] if seed % 2 and base else []

    placements, _ = placement_candidates(
        sheet.material, others, moving.piece, params, whole
    )

    assert placements
    for p in placements:
        w, h = (
            (moving.piece.height, moving.piece.width)
            if p.rotated
            else (moving.piece.width, moving.piece.height)
        )
        trial = PlacedPiece(moving.piece, p.x, p.y, w, h, p.rotated)
        kept = [r for i, r in enumerate(whole) if i not in p.uses_whole_offcuts]
        _, violations = realize_sheet(sheet.material, [*others, trial], params, kept)
        assert violations == [], (seed, p, violations)


def test_first_only_stops_at_one_position_per_orientation():
    placements, _ = placement_candidates(
        SHEET, [], Piece("Q", 200, 100), KERF_4, first_only=True
    )

    assert sorted(p.rotated for p in placements) == [False, True]


# ---------------------------------------------------------------------------
# Growing an offcut
# ---------------------------------------------------------------------------


def test_the_small_offcut_beside_a_piece_can_grow_to_full_height():
    corner = _at(Piece("P", 400, 300), 0, 0)
    beside = Rectangle(404.0, 0.0, 596.0, 300.0)

    extensions = dict(
        (d, (r.x, r.y, r.width, r.height))
        for d, r in leftover_extensions(SHEET, [corner], KERF_4, [], beside)
    )

    assert extensions == {"y+": (404.0, 0.0, 596.0, 1000.0)}


def test_an_offcut_does_not_grow_through_a_piece():
    corner = _at(Piece("P", 400, 300), 0, 0)
    above = _at(Piece("A", 300, 200), 600, 500)
    beside = Rectangle(404.0, 0.0, 596.0, 300.0)

    extensions = dict(leftover_extensions(SHEET, [corner, above], KERF_4, [], beside))

    grown = extensions["y+"]
    assert grown.y + grown.height == 496.0  # a blade short of the piece above


@pytest.mark.parametrize("seed", range(6))
def test_a_piece_can_always_go_back_where_it_was(seed):
    """The corners of the free space miss about one piece in seven; the piece's
    own spot is offered anyway, and first."""
    rng = random.Random(seed)
    params = CuttingParameters(
        kerf=4, left_trim=10, right_trim=10, top_trim=10, bottom_trim=10
    )
    pieces = [
        Piece(f"p{i}", rng.randint(150, 800), rng.randint(150, 800))
        for i in range(rng.randint(8, 20))
    ]
    layouts, _ = optimize_bins(
        pieces,
        [BinSpec(key="b", width=2100, height=2440, thickness=15, cost_per_unit=1)],
        params,
    )
    for layout in layouts:
        for pp in layout.placed_pieces:
            others = [o for o in layout.placed_pieces if o is not pp]
            here = Placement(pp.x, pp.y, pp.rotated)
            placements, _ = placement_candidates(
                layout.material, others, pp.piece, params, current=here
            )
            assert placements[0] == here, (seed, pp.piece.id)
            assert len({(p.x, p.y, p.rotated) for p in placements}) == len(placements)


def test_a_piece_turned_on_its_own_corner_is_offered_when_it_fits():
    piece = Piece("P", 300, 200)
    here = Placement(0.0, 0.0, False)

    placements, _ = placement_candidates(SHEET, [], piece, KERF_4, current=here)

    assert placements[:2] == [here, Placement(0.0, 0.0, True)]


def test_a_turn_that_would_hit_a_neighbour_is_not_offered_on_the_spot():
    piece = Piece("P", 300, 200)
    here = Placement(0.0, 0.0, False)

    clear = _at(Piece("B", 300, 900), 304, 0)
    placements, _ = placement_candidates(SHEET, [clear], piece, KERF_4, current=here)
    assert placements[0] == here
    assert Placement(0.0, 0.0, True) in placements  # turned it is 200 wide: clears B

    # Right above it: clear of the piece as it lies (200 high), in the way of the
    # piece turned (300 high).
    above = _at(Piece("C", 300, 100), 0, 204)
    placements, _ = placement_candidates(SHEET, [above], piece, KERF_4, current=here)
    assert placements[0] == here
    assert Placement(0.0, 0.0, True) not in placements
