"""Unit: the workshop board's material projection (no DB).

``_board_usage``/``_banding_usage`` are what the shop floor reads before taking
an order — "do I have this material?". They project the order's frozen snapshot,
so they are pure functions over dicts and testable without Postgres.

The snapshots are built by running the real ``build_materials_summary`` over
synthetic layouts rather than by hand-writing summary rows: the whole point of
``_board_usage`` is to undo that function's ``(material_key, half_board)``
grouping, so pinning the two against each other is what keeps them honest.
"""

from src.modules.optimizations.summary import build_materials_summary
from src.modules.orders.service import _banding_usage, _board_usage


def _layout(key="b1", half=False, width=1220.0, cost=45.5):
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
        "statistics": {"used_area": 1_000_000.0},
        "remainders": [],
        "cuts": [],
    }


def _catalog(key="b1", name="Melamina Blanca 15mm", code="MEL0023"):
    return {
        "material_key": key,
        "source": "catalog",
        "product_id": 7,
        "product_code": code,
        "product_name": name,
        "width": 1220.0,
        "height": 2440.0,
        "thickness": 15.0,
        "cost_per_unit": 45.5,
    }


def _snapshot(layouts, materials):
    return {
        "materials": materials,
        "materials_summary": build_materials_summary(layouts, materials),
    }


def test_whole_and_half_of_one_material_are_a_single_row():
    """The case the shop complained about: "1 tablero y medio" is ONE product.

    ``materials_summary`` bills it as two lines; the rack holds one thing.
    """
    snapshot = _snapshot(
        [_layout(), _layout(), _layout(half=True, width=610.0, cost=25.03)],
        [_catalog()],
    )
    assert len(snapshot["materials_summary"]) == 2  # the billing view still splits

    assert _board_usage(snapshot) == [
        {
            "material_key": "b1",
            # No " (medio tablero)": the suffix is a billing label, and half of
            # this row is not a half board.
            "name": "Melamina Blanca 15mm",
            "count": 3,
            "full_count": 2,
            "half_count": 1,
        }
    ]


def test_a_material_billed_only_as_a_half_keeps_the_whole_boards_name():
    """A half-only material still names the product, not the half-board line."""
    snapshot = _snapshot([_layout(half=True, width=610.0, cost=25.03)], [_catalog()])
    (row,) = _board_usage(snapshot)
    assert row["name"] == "Melamina Blanca 15mm"
    assert (row["count"], row["full_count"], row["half_count"]) == (1, 0, 1)


def test_distinct_materials_stay_distinct():
    """Merging is per ``material_key``; two materials never collapse."""
    snapshot = _snapshot(
        [_layout(), _layout(key="b2"), _layout(key="b2", half=True, width=610.0)],
        [_catalog(), _catalog(key="b2", name="Melamina Nogal 18mm", code="MEL0088")],
    )
    rows = _board_usage(snapshot)
    assert [(r["material_key"], r["count"]) for r in rows] == [("b1", 1), ("b2", 2)]
    assert rows[1]["name"] == "Melamina Nogal 18mm"


def test_an_inline_material_is_named_by_the_whole_sheet():
    """An offcut/manual material has no product name, so it is named by its
    dimensions -- the FULL sheet's, read off ``materials``. Reading the summary
    line instead would print the already-halved ``610×2440``."""
    inline = {
        "material_key": "r1",
        "source": "companyOffcut",
        "product_id": None,
        "product_code": None,
        "product_name": None,
        "width": 1220.0,
        "height": 2440.0,
        "thickness": 15.0,
        "cost_per_unit": 0.0,
    }
    snapshot = _snapshot(
        [_layout(key="r1", half=True, width=610.0, cost=0.0)], [inline]
    )
    assert _board_usage(snapshot)[0]["name"] == "1220×2440"


def test_a_snapshot_without_materials_falls_back_to_the_summary_line():
    """Defensive: an order frozen without ``materials`` still names its rows."""
    snapshot = {"materials_summary": build_materials_summary([_layout()], [_catalog()])}
    assert _board_usage(snapshot)[0]["name"] == "Melamina Blanca 15mm"


def test_board_usage_of_an_empty_snapshot_is_empty():
    assert _board_usage({}) == []


def test_banding_usage_carries_the_type_as_data():
    """The type is a badge on the board, so it travels canonical and unfolded:
    ``"(Suave)"`` inside the name left the client unable to tell it from the
    product's own words."""
    snapshot = {
        "edge_bandings_summary": [
            {
                "product_id": 3,
                "product_name": "Tapacanto Blanco 22mm",
                "band_type": "Soft",
                "billed_linear_m": 12.4,
            },
            # Geometry-only banding (no product assigned yet): skipped, the shop
            # cannot fetch a roll that was never chosen.
            {"product_id": None, "product_name": None, "billed_linear_m": 3.0},
            # A product whose band type was never loaded still lists its meters.
            {
                "product_id": 9,
                "product_name": "Tapacanto Nogal 22mm",
                "band_type": None,
                "billed_linear_m": 3.2,
            },
        ]
    }
    assert _banding_usage(snapshot) == [
        {"name": "Tapacanto Blanco 22mm", "band_type": "Soft", "linear_m": 12.4},
        {"name": "Tapacanto Nogal 22mm", "band_type": None, "linear_m": 3.2},
    ]


def test_banding_usage_of_an_empty_snapshot_is_empty():
    assert _banding_usage({}) == []
