"""Unit: re-pricing a finished plan at the catalog's price level (no DB, no engine).

``apply_price_level`` is a pure payload→payload transform applied AFTER the
optimization cache — that is the whole point of it, since resolving the level up
front would put it in the hash and make a checkbox re-run the search. What it
must guarantee: only the marked catalog boards move, **the half board never
does** (it is billed off the list price at every level — the level is a
concession on the whole plank and the half already carries its own markup), the
totals and the summary follow, and it is a strict no-op when there is nothing to
change.

``level_discount`` reads the same plan back the other way round — how far below
the list price it landed — and its one hard case is the half board the client
took whole: it discounts nothing while it is a half, and the whole board's
discount the moment the promotion makes it a full sheet.
"""

from src.modules.optimizations.patterns import group_layouts
from src.modules.optimizations.price_levels import (
    apply_price_level,
    level_discount,
    price_at_level,
)
from src.modules.optimizations.summary import build_materials_summary
from src.modules.optimizations.whole_boards import apply_whole_boards

FULL_W = 1220.0
HALF_W = 610.0
H = 2440.0
LIST = 45.5
LEVEL_2 = 40.0
# 45.5 / 2 * 1.10, the formula ``_half_spec`` bills a half board by. The second
# one is what a half would cost if the level reached it — the number these tests
# exist to keep it away from.
HALF_LIST = 25.03
HALF_LEVEL_2_IF_IT_APPLIED = 22.0


def _material(key="b1", source="catalog", cost=LIST):
    return {
        "material_key": key,
        "source": source,
        "product_id": 7 if source == "catalog" else None,
        "product_code": "MEL0023",
        "product_name": "Melamina Blanca 15mm",
        "width": FULL_W,
        "height": H,
        "thickness": 15.0,
        "cost_per_unit": cost,
    }


def _layout(key="b1", half=False, sheet_number=1, cost=LIST):
    width = HALF_W if half else FULL_W
    return {
        "material": {
            "material_key": key,
            "sheet_number": sheet_number,
            "width": width,
            "height": H,
            "thickness": 15.0,
            "area": width * H,
            "cost_per_unit": cost,
            "half_board": half,
        },
        "placed_pieces": [
            {"piece_id": "p1", "x": 10.0, "y": 10.0, "width": 300.0, "height": 600.0}
        ],
        "statistics": {
            "used_area": 180_000.0,
            "waste_area": width * H - 180_000.0,
            "efficiency": 6.0,
            "pieces_count": 1,
            "cut_linear_m": 3.5,
            "edge_banding_linear_m": 1.2,
        },
        "remainders": [],
        "cuts": [],
    }


def _payload(layouts=None, materials=None, boards_cost=LIST):
    layouts = layouts if layouts is not None else [_layout()]
    materials = materials if materials is not None else [_material()]
    return {
        "total_boards_used": len(layouts),
        "total_boards_cost": boards_cost,
        "total_edge_banding_cost": 4.0,
        "materials": materials,
        "layouts": layouts,
        "materials_summary": build_materials_summary(layouts, materials),
        "layout_groups": group_layouts(layouts),
    }


# --- price_at_level -------------------------------------------------------------
def test_level_1_is_always_the_list_price():
    assert price_at_level(45.5, 40.0, 35.0, 1) == 45.5


def test_levels_2_and_3_read_their_own_price():
    assert price_at_level(45.5, 40.0, 35.0, 2) == 40.0
    assert price_at_level(45.5, 40.0, 35.0, 3) == 35.0


def test_a_level_the_vendor_never_loaded_falls_back_to_the_list_price():
    """0.000000 in the source becomes None here — never a board billed at $0."""
    assert price_at_level(45.5, None, None, 2) == 45.5
    assert price_at_level(45.5, 40.0, None, 3) == 45.5


# --- no-ops ---------------------------------------------------------------------
def test_no_marked_boards_returns_the_same_object():
    """The normal case (nothing marked) must not even copy the payload."""
    payload = _payload()
    assert apply_price_level(payload, {}) is payload


def test_a_level_priced_the_same_as_the_list_is_a_noop():
    """More than half the real catalog publishes the same number at every level."""
    payload = _payload()
    assert apply_price_level(payload, {"b1": LIST}) is payload


def test_an_unknown_material_key_is_skipped():
    payload = _payload()
    assert apply_price_level(payload, {"otro": 1.0}) is payload


def test_an_offcut_is_never_re_priced():
    """Its cost comes from the request, not from a catalog that has levels."""
    payload = _payload(
        layouts=[_layout(key="r1", cost=0.0)],
        materials=[_material(key="r1", source="companyOffcut", cost=0.0)],
        boards_cost=0.0,
    )
    assert apply_price_level(payload, {"r1": LEVEL_2}) is payload


# --- the re-pricing itself -------------------------------------------------------
def test_a_marked_board_is_re_priced_everywhere_it_appears():
    payload = _payload()
    result = apply_price_level(payload, {"b1": LEVEL_2})

    assert result is not payload  # the input is never mutated
    assert payload["total_boards_cost"] == LIST
    assert result["total_boards_cost"] == LEVEL_2
    assert result["layouts"][0]["material"]["cost_per_unit"] == LEVEL_2
    assert result["materials"][0]["cost_per_unit"] == LEVEL_2
    assert result["materials_summary"][0]["cost_per_unit"] == LEVEL_2
    assert result["materials_summary"][0]["total_cost"] == LEVEL_2
    # The representative layout inside the groups has to move too: after the
    # cache's JSON round trip it is a separate copy, and the render layer reads it.
    assert result["layout_groups"][0]["layout"]["material"]["cost_per_unit"] == LEVEL_2


def test_the_pieces_never_move():
    payload = _payload()
    result = apply_price_level(payload, {"b1": LEVEL_2})
    # Shared by reference, which is what makes "re-pricing cannot move a piece"
    # structural rather than a promise.
    assert (
        result["layouts"][0]["placed_pieces"] is payload["layouts"][0]["placed_pieces"]
    )


def test_a_half_board_never_takes_the_level():
    """The half is billed off the LIST price even on a marked board.

    The level is a concession on the whole plank, and the half already carries
    its markup because the shop keeps the other half — letting the level through
    would rebate the same board twice.
    """
    payload = _payload(
        layouts=[_layout(half=True, cost=HALF_LIST)],
        boards_cost=HALF_LIST,
    )
    result = apply_price_level(payload, {"b1": LEVEL_2})
    assert result["layouts"][0]["material"]["cost_per_unit"] == HALF_LIST
    assert result["layouts"][0]["material"]["cost_per_unit"] != (
        HALF_LEVEL_2_IF_IT_APPLIED
    )
    assert result["total_boards_cost"] == HALF_LIST
    # The sheet is carried over untouched, not rebuilt at the same price.
    assert result["layouts"][0] is payload["layouts"][0]
    # The material still moves: ``whole_boards`` reads it as the whole sheet's
    # price when the client takes the half whole.
    assert result["materials"][0]["cost_per_unit"] == LEVEL_2


def test_a_marked_material_moves_its_whole_sheets_and_not_its_half():
    """The two rules on one material, which is how a real quote comes out.

    A board cut as one full sheet plus a half yields two billing lines, and only
    the full one follows the level.
    """
    payload = _payload(
        layouts=[_layout(), _layout(half=True, sheet_number=2, cost=HALF_LIST)],
        boards_cost=round(LIST + HALF_LIST, 2),
    )
    result = apply_price_level(payload, {"b1": LEVEL_2})

    assert result["layouts"][0]["material"]["cost_per_unit"] == LEVEL_2
    assert result["layouts"][1]["material"]["cost_per_unit"] == HALF_LIST
    assert result["total_boards_cost"] == round(LEVEL_2 + HALF_LIST, 2)
    # One line each, keyed by ``(material_key, half_board)``.
    lines = {
        (row["material_key"], row["half_board"]): row["cost_per_unit"]
        for row in result["materials_summary"]
    }
    assert lines == {("b1", False): LEVEL_2, ("b1", True): HALF_LIST}


def test_only_the_marked_material_moves():
    payload = _payload(
        layouts=[_layout(key="b1"), _layout(key="b2", sheet_number=2)],
        materials=[_material(key="b1"), _material(key="b2")],
        boards_cost=LIST * 2,
    )
    result = apply_price_level(payload, {"b1": LEVEL_2})
    costs = {
        layout["material"]["material_key"]: layout["material"]["cost_per_unit"]
        for layout in result["layouts"]
    }
    assert costs == {"b1": LEVEL_2, "b2": LIST}
    assert result["total_boards_cost"] == round(LEVEL_2 + LIST, 2)


def test_the_total_is_a_delta_so_a_pooled_offcut_is_never_billed():
    """``total_boards_cost`` excludes pooled offcuts and ``pool_key`` doesn't
    survive into the payload, so re-summing the layouts here would start billing
    the client's own material."""
    payload = _payload(
        layouts=[_layout(key="b1"), _layout(key="r1", sheet_number=2, cost=0.0)],
        materials=[
            _material(key="b1"),
            _material(key="r1", source="clientOffcut", cost=0.0),
        ],
        # Only the catalog sheet is billed: the offcut is the client's.
        boards_cost=LIST,
    )
    result = apply_price_level(payload, {"b1": LEVEL_2})
    assert result["total_boards_cost"] == LEVEL_2


# --- level_discount ---------------------------------------------------------------
def test_a_marked_board_is_discounted_against_the_list_price():
    payload = apply_price_level(_payload(), {"b1": LEVEL_2})
    assert level_discount(payload, {"b1": LIST}) == round(LIST - LEVEL_2, 2)


def test_nothing_marked_is_no_discount():
    """Level 1, or a quote where the seller marked no board at all."""
    assert level_discount(_payload(), {}) == 0.0


def test_a_board_priced_the_same_at_every_level_discounts_nothing():
    """More than half the catalog, plus every level the vendor never loaded."""
    assert level_discount(_payload(), {"b1": LIST}) == 0.0


def test_an_unmarked_board_never_contributes():
    """It bills at list, so the caller leaves it out of the reference map."""
    payload = _payload(
        layouts=[_layout(key="b1"), _layout(key="b2", sheet_number=2)],
        materials=[_material(key="b1"), _material(key="b2")],
        boards_cost=LIST * 2,
    )
    payload = apply_price_level(payload, {"b1": LEVEL_2})
    assert level_discount(payload, {"b1": LIST}) == round(LIST - LEVEL_2, 2)


def test_the_clients_own_retazo_is_never_discounted():
    """It is not in the reference map, which is the whole filter: its cost comes
    from the request, not from a catalog that has levels."""
    payload = _payload(
        layouts=[_layout(key="b1"), _layout(key="r1", sheet_number=2, cost=0.0)],
        materials=[
            _material(key="b1"),
            _material(key="r1", source="clientOffcut", cost=0.0),
        ],
        boards_cost=LIST,
    )
    payload = apply_price_level(payload, {"b1": LEVEL_2})
    assert level_discount(payload, {"b1": LIST}) == round(LIST - LEVEL_2, 2)


def test_a_half_board_discounts_nothing():
    """It was never re-priced, so there is nothing for the client to save."""
    payload = _payload(
        layouts=[_layout(half=True, cost=HALF_LIST)], boards_cost=HALF_LIST
    )
    payload = apply_price_level(payload, {"b1": LEVEL_2})
    assert level_discount(payload, {"b1": LIST}) == 0.0


def test_a_half_board_taken_whole_is_discounted_as_a_whole_board():
    """The case that forces this to be measured on the FINAL payload.

    ``apply_whole_boards`` re-bills the sheet at the material's ``cost_per_unit``,
    which is by then the level's price, so the client who takes the board whole
    gets the whole board's discount — where the same sheet, left as a half,
    discounts nothing at all. Measured inside ``apply_price_level`` (before the
    promotion) it would report zero.
    """
    payload = _payload(
        layouts=[_layout(half=True, cost=HALF_LIST)], boards_cost=HALF_LIST
    )
    payload = apply_price_level(payload, {"b1": LEVEL_2})
    payload = apply_whole_boards(payload, ["b1"])

    assert payload["layouts"][0]["material"]["half_board"] is False
    assert payload["layouts"][0]["material"]["cost_per_unit"] == LEVEL_2
    assert level_discount(payload, {"b1": LIST}) == round(LIST - LEVEL_2, 2)
