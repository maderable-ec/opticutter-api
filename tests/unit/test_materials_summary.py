"""Unit: the billing lines built from the serialized layouts (no DB).

``build_materials_summary`` used to be a method of ``OptimizationService`` that
consumed ``CuttingLayout`` objects. It reads dicts now because it also runs
AFTER the cache (``whole_boards``), where those objects no longer exist — so
the merge rule lives in exactly one place. These tests pin that rule.
"""

from src.modules.optimizations.summary import build_materials_summary


def _layout(key="b1", half=False, width=1220.0, cost=45.5, used_area=1_000_000.0):
    height = 2440.0
    return {
        "material": {
            "material_key": key,
            "sheet_number": 1,
            "width": width,
            "height": height,
            "thickness": 15.0,
            "area": width * height,
            "cost_per_unit": cost,
            "half_board": half,
        },
        "placed_pieces": [],
        "statistics": {"used_area": used_area},
        "remainders": [],
        "cuts": [],
    }


def _catalog(key="b1"):
    return {
        "material_key": key,
        "source": "catalog",
        "product_id": 7,
        "product_code": "MEL0023",
        "product_name": "Melamina Blanca 15mm",
        "width": 1220.0,
        "height": 2440.0,
        "thickness": 15.0,
        "cost_per_unit": 45.5,
    }


def test_half_and_whole_of_the_same_material_are_two_lines():
    lines = build_materials_summary(
        [_layout(), _layout(half=True, width=610.0, cost=25.03)], [_catalog()]
    )
    assert len(lines) == 2
    whole, half = lines
    assert whole["half_board"] is False
    assert whole["product_name"] == "Melamina Blanca 15mm"
    assert half["half_board"] is True
    assert half["product_name"] == "Melamina Blanca 15mm (medio tablero)"
    # One material key for both, which is what makes a single discount check
    # cover the two lines.
    assert whole["material_key"] == half["material_key"] == "b1"
    assert half["cost_per_unit"] == 25.03


def test_repeated_sheets_accumulate_count_and_cost():
    lines = build_materials_summary([_layout(), _layout(), _layout()], [_catalog()])
    assert len(lines) == 1
    assert lines[0]["count"] == 3
    assert lines[0]["total_cost"] == round(3 * 45.5, 2)


def test_inline_material_falls_back_to_dims_label_and_key_as_code():
    inline = {
        "material_key": "r1",
        "source": "clientOffcut",
        "product_id": None,
        "product_code": None,
        "product_name": None,
        "width": 800.0,
        "height": 600.0,
        "thickness": 15.0,
        "cost_per_unit": 0.0,
    }
    lines = build_materials_summary(
        [_layout(key="r1", width=800.0, cost=0.0)], [inline]
    )
    assert lines[0]["product_code"] == "r1"
    assert lines[0]["product_name"] == "800×2440"
    assert lines[0]["product_id"] is None


def test_avg_efficiency_is_the_area_ratio_not_the_rounded_statistic():
    """Recomputed from the raw areas: ``statistics.efficiency`` arrives already
    rounded to 2 decimals, and averaging rounded values would shift every
    efficiency this module has ever reported."""
    area = 1220.0 * 2440.0
    lines = build_materials_summary(
        [_layout(used_area=1_000_000.0), _layout(used_area=2_000_000.0)], [_catalog()]
    )
    expected = round(((1_000_000.0 / area * 100) + (2_000_000.0 / area * 100)) / 2, 2)
    assert lines[0]["avg_efficiency"] == expected


def test_zero_area_does_not_divide_by_zero():
    layout = _layout()
    layout["material"]["area"] = 0.0
    assert build_materials_summary([layout], [_catalog()])[0]["avg_efficiency"] == 0.0
