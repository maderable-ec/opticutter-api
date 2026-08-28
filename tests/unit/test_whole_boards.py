"""Unit: promoting a half board to the whole board (no DB, no engine).

``apply_whole_boards`` is a pure payload→payload transform applied AFTER the
optimization cache, so it can be exercised on a payload built by hand. What it
must guarantee: the pieces never move, the uncut half becomes one clean
leftover plus its rip cut, the money and the metres follow, and the whole thing
is a strict no-op when nothing is marked.
"""

import copy

from src.modules.optimizations.whole_boards import apply_whole_boards

FULL_W = 1220.0
HALF_W = 610.0
H = 2440.0
FULL_COST = 45.5
HALF_COST = 25.03


def _material(key="b1", source="catalog", width=FULL_W, cost=FULL_COST):
    return {
        "material_key": key,
        "source": source,
        "product_id": 7,
        "product_code": "MEL0023",
        "product_name": "Melamina Blanca 15mm",
        "width": width,
        "height": H,
        "thickness": 15.0,
        "cost_per_unit": cost,
    }


def _layout(key="b1", half=True, sheet_number=1, used_area=180_000.0):
    width = HALF_W if half else FULL_W
    return {
        "material": {
            "material_key": key,
            "sheet_number": sheet_number,
            "width": width,
            "height": H,
            "thickness": 15.0,
            "area": width * H,
            "cost_per_unit": HALF_COST if half else FULL_COST,
            "half_board": half,
        },
        "placed_pieces": [
            {
                "piece_id": "p1",
                "x": 10.0,
                "y": 10.0,
                "width": 300.0,
                "height": 600.0,
                "rotated": False,
                "original_width": 300.0,
                "original_height": 600.0,
            }
        ],
        "statistics": {
            "used_area": used_area,
            "waste_area": width * H - used_area,
            "efficiency": round(used_area / (width * H) * 100, 2),
            "pieces_count": 1,
            "cut_linear_m": 3.5,
            "edge_banding_linear_m": 1.2,
        },
        "remainders": [{"x": 320.0, "y": 0.0, "width": 290.0, "height": H}],
        "cuts": [{"x": 320.0, "y": 0.0, "length": H, "is_horizontal": False}],
    }


def _payload(layouts=None, materials=None, boards_cost=HALF_COST):
    layouts = layouts if layouts is not None else [_layout()]
    materials = materials if materials is not None else [_material()]
    from src.modules.optimizations.patterns import group_layouts
    from src.modules.optimizations.summary import build_materials_summary

    return {
        "strategy": "default",
        "variant": 0,
        "total_boards_used": len(layouts),
        "total_boards_cost": boards_cost,
        "total_edge_banding_cost": 4.0,
        "total_cut_linear_m": 3.5,
        "total_edge_banding_linear_m": 1.2,
        "materials": materials,
        "requirements": [],
        "layouts": layouts,
        "materials_summary": build_materials_summary(layouts, materials),
        "edge_bandings_summary": [],
        "layout_groups": group_layouts(layouts),
    }


# --- no-ops ---------------------------------------------------------------------
def test_no_keys_returns_the_same_object():
    """The normal case (nothing marked) must not even copy the payload."""
    payload = _payload()
    assert apply_whole_boards(payload, set()) is payload


def test_marked_material_without_halves_is_a_noop():
    payload = _payload(layouts=[_layout(half=False)], boards_cost=FULL_COST)
    assert apply_whole_boards(payload, {"b1"}) is payload


def test_unknown_material_key_is_skipped():
    payload = _payload()
    assert apply_whole_boards(payload, {"otro"}) is payload


def test_non_catalog_material_is_skipped():
    """An offcut is never halved; if one somehow is, promoting it is refused."""
    payload = _payload(materials=[_material(source="clientOffcut")])
    assert apply_whole_boards(payload, {"b1"}) is payload


def test_sheet_already_as_wide_as_the_board_is_skipped():
    """Defensive: a "half" that isn't narrower has nothing to give back."""
    layout = _layout()
    layout["material"]["width"] = FULL_W
    payload = _payload(layouts=[layout])
    assert apply_whole_boards(payload, {"b1"}) is payload


def test_input_payload_is_never_mutated():
    payload = _payload()
    before = copy.deepcopy(payload)
    apply_whole_boards(payload, {"b1"})
    assert payload == before


# --- the promotion --------------------------------------------------------------
def test_sheet_becomes_full_width_and_price():
    result = apply_whole_boards(_payload(), {"b1"})
    material = result["layouts"][0]["material"]
    assert material["half_board"] is False
    assert material["width"] == FULL_W
    assert material["area"] == FULL_W * H
    assert material["cost_per_unit"] == FULL_COST
    # Untouched identity/geometry of the sheet itself.
    assert material["material_key"] == "b1"
    assert material["sheet_number"] == 1
    assert material["height"] == H
    assert material["thickness"] == 15.0


def test_pieces_are_not_moved():
    """The whole point of promoting instead of re-optimizing."""
    payload = _payload()
    result = apply_whole_boards(payload, {"b1"})
    assert (
        result["layouts"][0]["placed_pieces"] == payload["layouts"][0]["placed_pieces"]
    )


def test_adds_exactly_one_remainder_covering_the_untouched_half():
    payload = _payload()
    result = apply_whole_boards(payload, {"b1"})
    remainders = result["layouts"][0]["remainders"]
    assert len(remainders) == len(payload["layouts"][0]["remainders"]) + 1
    assert remainders[-1] == {
        "x": HALF_W,
        "y": 0.0,
        "width": FULL_W - HALF_W,
        "height": H,
    }


def test_adds_the_rip_cut_that_splits_the_board_first():
    payload = _payload()
    result = apply_whole_boards(payload, {"b1"})
    cuts = result["layouts"][0]["cuts"]
    assert len(cuts) == len(payload["layouts"][0]["cuts"]) + 1
    assert cuts[0] == {"x": HALF_W, "y": 0.0, "length": H, "is_horizontal": False}


def test_statistics_charge_the_delivered_half_as_waste():
    payload = _payload()
    before = payload["layouts"][0]["statistics"]["efficiency"]
    stats = apply_whole_boards(payload, {"b1"})["layouts"][0]["statistics"]
    assert stats["used_area"] == 180_000.0
    assert stats["pieces_count"] == 1
    assert stats["waste_area"] == FULL_W * H - 180_000.0
    assert stats["efficiency"] == round(180_000.0 / (FULL_W * H) * 100, 2)
    assert stats["efficiency"] < before


def test_cut_meters_grow_by_the_rip():
    result = apply_whole_boards(_payload(), {"b1"})
    assert result["layouts"][0]["statistics"]["cut_linear_m"] == round(3.5 + 2.44, 2)
    assert result["total_cut_linear_m"] == round(3.5 + 2.44, 2)
    assert result["layouts"][0]["statistics"]["edge_banding_linear_m"] == 1.2


def test_cost_delta_and_board_count():
    result = apply_whole_boards(_payload(), {"b1"})
    assert result["total_boards_cost"] == FULL_COST
    # A half already counted as one physical board.
    assert result["total_boards_used"] == 1


def test_summary_merges_the_half_line_into_the_whole_one():
    payload = _payload(
        layouts=[
            _layout(half=False, sheet_number=1),
            _layout(half=True, sheet_number=2),
        ],
        boards_cost=FULL_COST + HALF_COST,
    )
    assert len(payload["materials_summary"]) == 2
    summary = apply_whole_boards(payload, {"b1"})["materials_summary"]
    assert len(summary) == 1
    line = summary[0]
    assert line["count"] == 2
    assert line["half_board"] is False
    assert line["product_name"] == "Melamina Blanca 15mm"
    assert line["cost_per_unit"] == FULL_COST
    assert line["total_cost"] == round(2 * FULL_COST, 2)


def test_two_halves_of_the_same_material():
    payload = _payload(
        layouts=[_layout(sheet_number=1), _layout(sheet_number=2)],
        boards_cost=2 * HALF_COST,
    )
    result = apply_whole_boards(payload, {"b1"})
    assert all(x["material"]["half_board"] is False for x in result["layouts"])
    assert result["total_boards_cost"] == round(2 * FULL_COST, 2)
    assert result["total_cut_linear_m"] == round(3.5 + 2 * 2.44, 2)
    assert len(result["materials_summary"]) == 1
    assert result["materials_summary"][0]["count"] == 2


def test_layout_groups_are_rebuilt_from_the_promoted_layouts():
    """Catches the cache-hit/cold-path asymmetry: after the JSON round trip the
    grouped layout is a COPY, so mutating `layouts` alone would leave the
    diagram showing half a sheet."""
    payload = _payload()
    before = payload["layout_groups"][0]
    result = apply_whole_boards(payload, {"b1"})
    group = result["layout_groups"][0]
    assert group["layout"]["material"]["half_board"] is False
    assert group["layout"]["material"]["width"] == FULL_W
    assert group["pattern_id"] == before["pattern_id"]
    assert group["sheet_numbers"] == before["sheet_numbers"]
    assert group["count"] == before["count"]


def test_other_materials_are_left_alone():
    payload = _payload(
        layouts=[_layout(key="b1"), _layout(key="b2", sheet_number=1)],
        materials=[_material("b1"), _material("b2")],
        boards_cost=2 * HALF_COST,
    )
    result = apply_whole_boards(payload, {"b1"})
    by_key = {x["material"]["material_key"]: x for x in result["layouts"]}
    promoted, untouched = by_key["b1"], by_key["b2"]
    assert promoted["material"]["half_board"] is False
    assert untouched["material"]["half_board"] is True
    assert result["total_boards_cost"] == round(
        2 * HALF_COST + (FULL_COST - HALF_COST), 2
    )
