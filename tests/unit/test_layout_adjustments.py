"""Hand adjustments laid over the engine's payload (``layout_adjustments``)."""

import pytest

from src.cutting import CuttingParameters, Piece
from src.cutting.sheet_check import OVERLAP
from src.modules.optimizations.layout_adjustments import (
    DUPLICATE_PIECE,
    EDITING,
    LENIENT,
    OFFCUT_EXHAUSTED,
    PENDING_PIECES,
    SHEET_NOT_AVAILABLE,
    STRICT,
    UNKNOWN_PIECE,
    LayoutAdjustmentError,
    PoolSpec,
    SheetBin,
    apply_layout_adjustments,
    placed_pieces,
    realize_sheet,
    sheets_from_layouts,
)
from src.modules.optimizations.schemas import LayoutAdjustment
from src.modules.optimizations.summary import build_materials_summary

KERF_4 = CuttingParameters(kerf=4)
BOARD = SheetBin("b", False, 1000, 1000, 18, 40.0, None, "Tablero")
HALF = SheetBin("b", True, 500, 1000, 18, 23.0, None, "Tablero (medio tablero)")
OFFCUT = SheetBin("r", False, 600, 600, 18, 0.0, 1, "Retazo")
PIECES = [Piece(f"P#{i}", 300, 200) for i in range(1, 5)]


def _serialize(layout):
    data = layout.to_dict()
    data["statistics"]["cut_linear_m"] = round(layout.cut_length / 1000.0, 2)
    data["statistics"]["edge_banding_linear_m"] = 0.0
    return data


def _spec(bins=(BOARD, HALF), finite=False, pieces=PIECES):
    return PoolSpec(
        pool_key="b",
        label="«Tablero»",
        pieces={p.id: p for p in pieces},
        params=KERF_4,
        bins={(b.material_key, b.half_board): b for b in bins},
        finite=finite,
        serialize=_serialize,
        min_usable_offcut=150.0,
    )


def _sheet(material_key="b", half=False, pieces=(), whole=()):
    return {
        "materialKey": material_key,
        "halfBoard": half,
        "pieces": [
            {"pieceId": pid, "x": x, "y": y, "rotated": r} for pid, x, y, r in pieces
        ],
        "wholeOffcuts": [dict(zip(("x", "y", "width", "height"), r)) for r in whole],
    }


def _adjustment(*sheets):
    return LayoutAdjustment.model_validate({"poolKey": "b", "sheets": list(sheets)})


# The engine's plan for the tests: the four pieces in a 2x2 block on one board.
BASE_SHEET = _sheet(
    pieces=[
        ("P#1", 0, 0, False),
        ("P#2", 304, 0, False),
        ("P#3", 0, 204, False),
        ("P#4", 304, 204, False),
    ]
)


def _base_payload(spec):
    adjustment = _adjustment(BASE_SHEET)
    layouts = []
    for sheet in adjustment.sheets:
        layout, violations = realize_sheet(
            spec.bins[(sheet.material_key, sheet.half_board)].material(),
            placed_pieces(sheet, spec),
            spec.params,
        )
        assert not violations
        layouts.append(_serialize(layout))
    materials = [{"material_key": "b", "source": "manual", "product_name": "Tablero"}]
    return {
        "layouts": layouts,
        "materials": materials,
        "unplaced": [],
        "total_boards_used": 1,
        "total_boards_cost": 40.0,
        "total_cut_linear_m": layouts[0]["statistics"]["cut_linear_m"],
        "total_edge_banding_linear_m": 0.0,
        "materials_summary": build_materials_summary(layouts, materials),
        "layout_groups": [],
    }


def _apply(adjustments, spec=None, mode=LENIENT, finite_keys=(), payload=None):
    spec = spec or _spec()
    payload = payload if payload is not None else _base_payload(spec)
    result, realized, issues = apply_layout_adjustments(
        payload,
        adjustments,
        {"b": spec},
        ["b"],
        {key: "b" for key, _ in spec.bins},
        set(finite_keys),
        mode,
    )
    return payload, result, realized, issues


def test_no_adjustment_hands_back_the_very_same_payload():
    spec = _spec()
    payload = _base_payload(spec)

    result, realized, issues = apply_layout_adjustments(
        payload, None, {}, ["b"], {}, set(), LENIENT
    )

    assert result is payload and realized == {} and issues == []


def test_an_adjustment_equal_to_the_plan_reuses_every_sheet_verbatim():
    payload, result, realized, _ = _apply(
        [
            LayoutAdjustment(
                pool_key="b",
                sheets=sheets_from_layouts(_base_payload(_spec())["layouts"]),
            )
        ]
    )

    assert "b" in realized
    assert result["layouts"] == payload["layouts"]
    assert not any(layout.get("adjusted") for layout in result["layouts"])
    assert result["total_boards_used"] == 1


def test_moving_a_piece_onto_a_new_sheet_counts_a_board_and_flags_the_piece():
    moved = _sheet(
        pieces=[("P#1", 0, 0, False), ("P#2", 304, 0, False), ("P#3", 0, 204, False)]
    )
    alone = _sheet(half=True, pieces=[("P#4", 0, 0, False)])

    _, result, _, issues = _apply([_adjustment(moved, alone)])

    assert issues == []
    assert result["total_boards_used"] == 2
    assert result["total_boards_cost"] == 63.0
    flagged = {
        p["piece_id"]
        for layout in result["layouts"]
        for p in layout["placed_pieces"]
        if p.get("adjusted")
    }
    assert flagged == {"P#4"}
    assert [layout["material"]["half_board"] for layout in result["layouts"]] == [
        False,
        True,
    ]
    assert result["layout_adjustments"][0]["pool_key"] == "b"


def test_a_pending_piece_is_refused_on_write_but_allowed_while_editing():
    three = _sheet(
        pieces=[("P#1", 0, 0, False), ("P#2", 304, 0, False), ("P#3", 0, 204, False)]
    )

    with pytest.raises(LayoutAdjustmentError) as refused:
        _apply([_adjustment(three)], mode=STRICT)
    assert refused.value.issues[0].code == PENDING_PIECES
    assert "P#4" in refused.value.detail

    _, result, realized, _ = _apply([_adjustment(three)], mode=EDITING)
    assert realized["b"].pending[0].id == "P#4"
    assert result["unplaced"] == [
        {
            "material_key": "b",
            "label": "P",
            "width": 300.0,
            "height": 200.0,
            "quantity": 1,
        }
    ]


def test_a_read_falls_back_to_the_engine_when_the_adjustment_no_longer_holds():
    crossed = _sheet(
        pieces=[
            ("P#1", 0, 0, False),
            ("P#2", 100, 0, False),
            ("P#3", 0, 204, False),
            ("P#4", 304, 204, False),
        ]
    )

    payload, result, realized, issues = _apply([_adjustment(crossed)])

    assert realized == {}
    assert result["layouts"] == payload["layouts"]
    assert [i.code for i in issues] == [OVERLAP]
    assert result["layout_issues"][0]["message"] == (
        "«P#1» se cruza con «P#2» en la hoja 1 de «Tablero»."
    )


def test_the_same_problem_raises_while_editing():
    crossed = _sheet(
        pieces=[
            ("P#1", 0, 0, False),
            ("P#2", 100, 0, False),
            ("P#3", 0, 204, False),
            ("P#4", 304, 204, False),
        ]
    )

    with pytest.raises(LayoutAdjustmentError) as refused:
        _apply([_adjustment(crossed)], mode=EDITING)

    assert refused.value.field == "layoutAdjustments"


@pytest.mark.parametrize(
    "sheets, code",
    [
        (
            [_sheet(pieces=[("P#9", 0, 0, False)]), BASE_SHEET],
            UNKNOWN_PIECE,
        ),
        (
            [BASE_SHEET, _sheet(half=True, pieces=[("P#1", 0, 0, False)])],
            DUPLICATE_PIECE,
        ),
    ],
)
def test_ids_that_do_not_match_the_cut_list_are_reported(sheets, code):
    _, _, realized, issues = _apply([_adjustment(*sheets)])

    assert realized == {}
    assert code in {i.code for i in issues}


def test_a_half_board_the_material_no_longer_has_is_reported():
    halves = _sheet(half=True, pieces=[("P#1", 0, 0, False), ("P#2", 0, 204, False)])
    rest = _sheet(pieces=[("P#3", 0, 0, False), ("P#4", 304, 0, False)])

    _, _, realized, issues = _apply(
        [_adjustment(rest, halves)], spec=_spec(bins=(BOARD,))
    )

    assert realized == {}
    assert [(i.code, i.sheet_index) for i in issues] == [(SHEET_NOT_AVAILABLE, 1)]


def test_an_offcut_cannot_be_used_more_times_than_there_are():
    spec = _spec(bins=(BOARD, OFFCUT))
    first = _sheet("r", pieces=[("P#1", 0, 0, False)])
    second = _sheet("r", pieces=[("P#2", 0, 0, False)])
    rest = _sheet(pieces=[("P#3", 0, 0, False), ("P#4", 304, 0, False)])

    _, _, realized, issues = _apply([_adjustment(rest, first, second)], spec=spec)

    assert realized == {}
    assert [i.code for i in issues] == [OFFCUT_EXHAUSTED]


def test_a_finite_pool_may_leave_pieces_unplaced_even_on_a_read():
    spec = _spec(bins=(OFFCUT,), finite=True)
    only = _adjustment(_sheet("r", pieces=[("P#1", 0, 0, False)]))
    nothing_placed = {"layouts": [], "materials": [], "unplaced": []}

    _, result, realized, issues = _apply(
        [only], spec=spec, finite_keys={"r"}, payload=nothing_placed
    )

    assert issues == [] and "b" in realized
    assert result["total_boards_used"] == 0
    assert result["unplaced"][0]["quantity"] == 3


def test_an_empty_sheet_is_neither_cut_nor_billed_nor_part_of_the_plan():
    empty = _sheet(half=True)

    _, result, realized, _ = _apply([_adjustment(BASE_SHEET, empty)])

    assert len(result["layouts"]) == 1
    assert [layout for _, layout in realized["b"].entries] == [
        result["layouts"][0],
        None,
    ]
    assert len(result["layout_adjustments"][0]["sheets"]) == 1


def test_a_whole_offcut_is_flagged_on_the_sheet():
    kept = _sheet(
        pieces=[
            ("P#1", 0, 0, False),
            ("P#2", 304, 0, False),
            ("P#3", 0, 204, False),
            ("P#4", 304, 204, False),
        ],
        whole=[(608, 0, 392, 1000)],
    )

    _, result, _, issues = _apply([_adjustment(kept)])

    assert issues == []
    flagged = [r for r in result["layouts"][0]["remainders"] if r.get("kept_whole")]
    assert flagged == [
        {"x": 608.0, "y": 0.0, "width": 392.0, "height": 1000.0, "kept_whole": True}
    ]
    assert result["layouts"][0]["adjusted"] is True
