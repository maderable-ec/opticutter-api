"""A hard tape cuts its piece 1 mm short per side, across the side it is on."""

from types import SimpleNamespace

from src.modules.optimizations.hard_edges import (
    HARD_EDGE_CUT_MM,
    cut_size,
    frozen_band_types,
    requirement_band_types,
    requirement_cut_size,
)
from src.modules.optimizations.schemas import Requirement
from src.modules.optimizations.visualization import _cut_short, _legend_entries


def _req(**kw):
    fields = dict(materialKey="m", height=600, width=400, quantity=1, priority=0)
    fields.update(kw)
    return Requirement.model_validate(fields)


def _tape(band_type):
    return SimpleNamespace(attributes={"bandType": band_type} if band_type else {})


def test_the_rule_is_one_millimetre_per_side():
    assert HARD_EDGE_CUT_MM == 1


def test_a_long_side_shortens_the_width_and_a_short_side_the_height():
    # The tape on a long side (L, left/right) adds across the ANCHO.
    assert cut_size(600, 400, {"left": "Hard"}) == (600, 399)
    assert cut_size(600, 400, {"left": "Hard", "right": "Hard"}) == (600, 398)
    # ... and on a short side (C, top/bottom) across the LARGO.
    assert cut_size(600, 400, {"top": "Hard"}) == (599, 400)
    assert cut_size(
        600, 400, {s: "Hard" for s in ("top", "bottom", "left", "right")}
    ) == (598, 398)


def test_a_soft_or_unknown_tape_takes_nothing_off():
    assert cut_size(600, 400, {"left": "Soft", "top": None}) == (600, 400)
    assert cut_size(600, 400, {}) == (600, 400)


def test_a_piece_too_small_for_its_discount_keeps_its_size():
    # Coerced, never raised: a pre-order re-validates on every read.
    assert cut_size(2, 1, {s: "Hard" for s in ("top", "bottom", "left")}) == (2, 1)


def test_a_requirement_reads_each_side_off_its_tapes_product():
    req = _req(
        edgeBanding={"sides": ["left", "right", "top"], "productId": 1},
        specialEdges=[{"side": "bottom", "productId": 2}],
    )
    tapes = {1: _tape("Soft"), 2: _tape("Hard")}
    assert requirement_band_types(req, tapes) == {
        "left": "Soft",
        "right": "Soft",
        "top": "Soft",
        "bottom": "Hard",
    }
    assert requirement_cut_size(req, tapes) == (599, 400)
    # The auto tape hard, the special one soft: the three auto sides go.
    tapes = {1: _tape("Hard"), 2: _tape("Soft")}
    assert requirement_cut_size(req, tapes) == (599, 398)


def test_a_tape_with_no_product_yet_is_not_hard():
    # Geometry-only banding: the product is assigned when quoting.
    req = _req(edgeBanding={"sides": ["left", "right"]})
    assert requirement_cut_size(req, {}) == (600, 400)
    assert requirement_cut_size(_req(), {1: _tape("Hard")}) == (600, 400)


def test_frozen_edges_let_a_special_edge_win_its_side():
    # An order frozen while a special edge still REPLACED the auto tape lists
    # the side in both; the special one is the tape that side got.
    edges = {
        "sides": ["left", "right"],
        "band_type": "Hard",
        "special_edges": [{"side": "right", "band_type": "Soft"}],
    }
    assert frozen_band_types(edges) == {"left": "Hard", "right": "Soft"}
    assert cut_size(600, 400, frozen_band_types(edges)) == (600, 399)
    assert frozen_band_types(None) == {}


# --------------------------------------------------------------------------- #
# The diagram's legend says when the numbers are the cut
# --------------------------------------------------------------------------- #
def _placed(height, width, original_height, original_width, rotated=False):
    return {
        "height": height,
        "width": width,
        "original_height": original_height,
        "original_width": original_width,
        "rotated": rotated,
    }


def test_a_piece_is_cut_short_when_its_placed_size_is_not_the_ordered_one():
    assert _cut_short(_placed(600, 398, 600, 400))
    # Rotated: the placed box is turned, the ordered size is not.
    assert _cut_short(_placed(398, 600, 600, 400, rotated=True))
    assert not _cut_short(_placed(400, 600, 600, 400, rotated=True))
    assert not _cut_short(_placed(600, 400, 600, 400))
    # A snapshot with no ``original_*`` at all is not cut short.
    assert not _cut_short({"height": 600, "width": 400})


def test_the_hard_entry_names_the_discount_only_when_it_was_taken():
    def hard_text(cut_short):
        [entry] = [e for e in _legend_entries({"Hard"}, cut_short) if e[4] == "hatch"]
        return entry[3]

    assert hard_text(False) == "Canto duro"
    assert hard_text(True) == "Canto duro (corte -1 mm por lado)"
