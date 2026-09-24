"""Why a piece is left out of the plan, and the message a write is refused with.

Pre-order 157 is the case: 2800×2070 boards trimmed 10 mm a side (2780×2050
useful), four pieces of 2785 mm that fit only untrimmed and one of 2150×2080
that fits in no orientation.
"""

from src.modules.optimizations.unplaced import (
    PoolGeometry,
    explain_unplaced,
    format_mm,
    unplaced_message,
)

ROBLE = "MDP RH ROBLE BARROCO DORADO (2.80X2.07)M-15MM"
TRIMS = (10.0, 10.0, 10.0, 10.0)


def _board(name=ROBLE, trims=TRIMS, rotatable=frozenset(), sheets=((2800, 2070),)):
    return PoolGeometry(name=name, sheets=sheets, trims=trims, rotatable=rotatable)


def _entry(height, width, quantity=1, key="b1", label=None):
    return {
        "material_key": key,
        "label": label,
        "height": height,
        "width": width,
        "quantity": quantity,
    }


def _reason(entry, pool):
    [explained] = explain_unplaced([entry], {entry["material_key"]: pool})
    return explained["reason"]


def test_a_piece_that_fits_only_untrimmed_says_so():
    [explained] = explain_unplaced([_entry(2785, 375, 2)], {"b1": _board()})

    assert explained["reason"] == "larger_than_trimmed_sheet"
    assert explained["material_name"] == ROBLE
    assert (explained["usable_height"], explained["usable_width"]) == (2780, 2050)
    # The engine's fields ride through untouched.
    assert explained["quantity"] == 2


def test_a_piece_larger_than_the_sheet_itself():
    assert _reason(_entry(2150, 2080), _board()) == "larger_than_sheet"


def test_with_the_trims_off_the_useful_area_is_the_whole_sheet():
    [explained] = explain_unplaced(
        [_entry(2900, 375)], {"b1": _board(trims=(0.0, 0.0, 0.0, 0.0))}
    )

    assert explained["reason"] == "larger_than_sheet"
    assert (explained["usable_height"], explained["usable_width"]) == (2800, 2070)


def test_turning_counts_only_for_a_piece_that_may_turn():
    # 2040×2700 is too wide as it stands and fits turned.
    fixed = _board()
    turnable = _board(rotatable=frozenset({(2040, 2700)}))

    assert _reason(_entry(2040, 2700), fixed) == "larger_than_sheet"
    assert _reason(_entry(2040, 2700), turnable) == "out_of_stock"


def test_a_piece_the_size_of_the_useful_area_fits():
    # No kerf around a piece with nothing beside it: it is what the packer does.
    assert _reason(_entry(2780, 2050), _board()) == "out_of_stock"


def test_a_finite_pool_is_checked_against_every_retazo_in_it():
    # The anchor is small, the pooled retazo is not: the piece fits the pool,
    # so what is missing is material, not a smaller piece.
    pool = PoolGeometry(
        name="retazo 600×600 mm",
        sheets=((600, 600), (1200, 1200)),
        trims=TRIMS,
        rotatable=frozenset(),
    )

    [explained] = explain_unplaced([_entry(900, 900)], {"b1": pool})

    assert explained["reason"] == "out_of_stock"
    # The useful area reported is the anchor's, the sheet the seller picked.
    assert (explained["usable_height"], explained["usable_width"]) == (580, 580)


def test_a_piece_the_hand_adjustment_left_out_is_pending():
    # It fits: nothing ran out, the seller has not placed it yet.
    pool = PoolGeometry(
        name=ROBLE,
        sheets=((2800, 2070),),
        trims=TRIMS,
        rotatable=frozenset(),
        adjusted=True,
    )

    explained = explain_unplaced([_entry(450, 450)], {"b1": pool})

    assert explained[0]["reason"] == "pending"
    assert "quedó fuera del ajuste manual" in unplaced_message(explained)
    # Still classified by size first: an adjustment cannot make a piece fit.
    assert _reason(_entry(2900, 450), pool) == "larger_than_sheet"


def test_an_unknown_pool_is_passed_through():
    [explained] = explain_unplaced([_entry(10, 10, key="zz")], {"b1": _board()})

    assert explained == _entry(10, 10, key="zz")


def test_the_message_names_every_piece_its_board_and_why():
    cashmere = "MDP RH CASHMERE (2.80X2.07)M-15MM"
    explained = explain_unplaced(
        [
            _entry(2785, 375, 2, label="piece_1"),
            _entry(2785, 255, 2, label="piece_3"),
            _entry(2150, 2080, 1, key="b2", label="piece_6"),
        ],
        {"b1": _board(), "b2": _board(name=cashmere)},
    )

    assert unplaced_message(explained) == (
        "Hay 5 piezas que no entran en el material y quedarían sin cortar: "
        f"2 de 2785×375 mm y 2 de 2785×255 mm en {ROBLE} "
        "(área útil 2780×2050 mm; entrarían sin refilar); "
        f"1 de 2150×2080 mm en {cashmere} (área útil 2780×2050 mm). "
        "Corrige las medidas, cambia el material o quita esas piezas."
    )


def test_the_message_uses_the_sellers_label_and_the_singular():
    explained = explain_unplaced(
        [_entry(2785, 375, 1, label="Lateral")], {"b1": _board()}
    )

    message = unplaced_message(explained)

    assert message.startswith(
        "Hay 1 pieza que no entra en el material y quedaría sin cortar: "
    )
    assert "1 «Lateral» de 2785×375 mm" in message
    assert "entraría sin refilar" in message


def test_the_message_for_retazos_that_ran_out():
    pool = PoolGeometry(
        name="Retazo grande",
        sheets=((1000, 1000),),
        trims=(0.0, 0.0, 0.0, 0.0),
        rotatable=frozenset(),
    )
    explained = explain_unplaced([_entry(900, 900, label="Puerta")], {"b1": pool})

    assert "1 «Puerta» de 900×900 mm en Retazo grande (no alcanza el material" in (
        unplaced_message(explained)
    )


def test_measurements_print_the_way_the_shop_writes_them():
    assert format_mm(2785.0) == "2785"
    assert format_mm(2785.5) == "2785.5"
