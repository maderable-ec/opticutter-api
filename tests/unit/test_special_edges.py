"""Unit: cantos especiales, the per-side tapes that win over the auto banding.

No DB. What is load-bearing, and silent when wrong:

- the precedence has ONE definition (``Requirement.side_products``): a special
  edge replaces the auto tape on its side and adds the side when the auto
  banding did not cover it, leaving every other side on the auto tape;
- a requirement with no special edge dumps, hashes, bills and draws exactly as
  it did before the field existed -- a new key would empty Redis on deploy;
- each side is billed to the tape it actually gets;
- the notation reads ``auto · specials`` in the auto banding's own count
  notation, one group per tape, and is unchanged without specials.
"""

import pytest
from pydantic import ValidationError

from src.modules.optimizations.labels import edge_banding_notation, edge_notation
from src.modules.optimizations.schemas import Requirement
from src.modules.optimizations.service import (
    OptimizationService,
    hashable_requirements,
)
from src.modules.optimizations.visualization import _side_band_types
from src.modules.orders.service import _piece_edges
from src.modules.products.model import ProductModel


def _req(edge_banding=None, special_edges=(), **overrides) -> Requirement:
    data = {
        "priority": 0,
        "height": 700,
        "width": 400,
        "quantity": 1,
        "material_key": "b1",
        "label": "Puerta",
        "edge_banding": edge_banding,
        "special_edges": list(special_edges),
    }
    data.update(overrides)
    return Requirement(**data)


def _tape(pid, band_type, alias, price=1.0) -> ProductModel:
    return ProductModel(
        id=pid,
        code=f"T{pid}",
        name=f"Tapacanto {alias}",
        type="edge_banding",
        price=price,
        alias=alias,
        attributes={"bandType": band_type, "color": "Blanco", "thickness": 0.45},
    )


_AUTO = {"sides": ["left", "right", "top"], "product_id": 7}
_TAPES = {7: _tape(7, "Soft", "CSH", price=1.0), 9: _tape(9, "Hard", "BLN", price=3.0)}


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #
def test_without_special_edges_every_auto_side_keeps_the_auto_tape():
    assert _req(_AUTO).side_products() == {"left": 7, "right": 7, "top": 7}


def test_a_special_edge_replaces_the_auto_tape_on_its_side_only():
    req = _req(_AUTO, [{"side": "left", "product_id": 9}])
    assert req.side_products() == {"left": 9, "right": 7, "top": 7}


def test_a_special_edge_adds_a_side_the_auto_banding_did_not_cover():
    req = _req(_AUTO, [{"side": "bottom", "product_id": 9}])
    assert req.side_products() == {"left": 7, "right": 7, "top": 7, "bottom": 9}


def test_special_edges_band_a_piece_with_no_auto_banding():
    req = _req(None, [{"side": "top", "product_id": 9}])
    assert req.side_products() == {"top": 9}


def test_geometry_only_auto_sides_stay_unpriced_next_to_a_special_edge():
    req = _req({"sides": ["left", "right"]}, [{"side": "right", "product_id": 9}])
    assert req.side_products() == {"left": None, "right": 9}


def test_a_side_takes_one_special_edge_at_most():
    with pytest.raises(ValidationError, match="must not repeat a side"):
        _req(
            _AUTO,
            [{"side": "left", "product_id": 9}, {"side": "left", "product_id": 7}],
        )


def test_a_special_edge_needs_a_product():
    with pytest.raises(ValidationError):
        _req(_AUTO, [{"side": "left"}])
    with pytest.raises(ValidationError):
        _req(_AUTO, [{"side": "left", "product_id": 0}])


def test_special_edges_arrive_camel_cased():
    req = Requirement.model_validate(
        {
            "priority": 0,
            "height": 700,
            "width": 400,
            "materialKey": "b1",
            "specialEdges": [{"side": "left", "productId": 9}],
        }
    )
    assert req.side_products() == {"left": 9}


# --------------------------------------------------------------------------- #
# Notation
# --------------------------------------------------------------------------- #
def test_the_notation_without_special_edges_is_the_old_one():
    for sides in (
        ["left"],
        ["left", "right", "top"],
        ["top", "bottom", "left", "right"],
    ):
        assert edge_notation(sides, "Soft", "CSH") == edge_banding_notation(
            sides, "Soft", "CSH"
        )


def test_the_auto_part_counts_only_the_sides_no_special_edge_took():
    special = [{"side": "left", "band_type": "Hard", "alias": "BLN"}]
    notation = edge_notation(["left", "right", "top"], "Soft", "CSH", special)
    assert notation == "1L1C CS CSH · 1L CD BLN"


def test_special_edges_are_counted_per_tape_long_sides_first():
    special = [
        {"side": "bottom", "band_type": "Soft", "alias": "CHM"},
        {"side": "left", "band_type": "Hard", "alias": "BLN"},
    ]
    assert edge_notation([], None, None, special) == "1L CD BLN · 1C CS CHM"


def test_sides_sharing_a_tape_read_as_one_count():
    """``2L CS BLN`` in, ``2L CS BLN`` out: the notation the seller typed."""
    bln = {"band_type": "Soft", "alias": "BLN"}
    both_long = [{"side": "left", **bln}, {"side": "right", **bln}]
    assert edge_notation(["top"], "Soft", "CSH", both_long) == "1C CS CSH · 2L CS BLN"
    long_and_short = [{"side": "top", **bln}, {"side": "left", **bln}]
    assert edge_notation([], None, None, long_and_short) == "1L1C CS BLN"
    everything = [{"side": s, **bln} for s in ("left", "right", "top", "bottom")]
    assert edge_notation([], None, None, everything) == "4L CS BLN"


def test_two_tapes_on_the_long_sides_read_as_two_counts():
    special = [
        {"side": "left", "band_type": "Soft", "alias": "BLN"},
        {"side": "right", "band_type": "Soft", "alias": "CHM"},
    ]
    assert edge_notation([], None, None, special) == "1L CS BLN · 1L CS CHM"


# --------------------------------------------------------------------------- #
# Hash and cached payload
# --------------------------------------------------------------------------- #
def test_no_special_edge_leaves_no_key_in_the_hash():
    [dumped] = hashable_requirements([_req(_AUTO)])
    assert "special_edges" not in dumped


def test_special_edges_are_in_the_hash_when_set():
    plain = hashable_requirements([_req(_AUTO)])
    special = hashable_requirements([_req(_AUTO, [{"side": "left", "product_id": 9}])])
    assert special != plain
    assert special[0]["special_edges"] == [{"side": "left", "product_id": 9}]


def test_the_cached_requirement_carries_each_special_edge_tape():
    data = OptimizationService._dump_requirement(
        _req(_AUTO, [{"side": "left", "product_id": 9}]), {}, _TAPES
    )
    assert data["special_edges"] == [
        {"side": "left", "product_id": 9, "band_type": "Hard", "alias": "BLN"}
    ]
    assert "special_edges" not in OptimizationService._dump_requirement(
        _req(_AUTO), {}, _TAPES
    )


# --------------------------------------------------------------------------- #
# Metres, per tape
# --------------------------------------------------------------------------- #
def _summary(requirements, mock_session):
    svc = OptimizationService(mock_session)
    rows, total = svc._build_edge_bandings_summary(requirements, _TAPES, 0.0)
    return {r["product_id"]: r["net_linear_m"] for r in rows}, total


def test_each_side_is_billed_to_the_tape_it_gets(mock_session):
    # 700 x 400, qty 2: left/right are 700, top/bottom 400.
    req = _req(
        _AUTO,
        [{"side": "left", "product_id": 9}, {"side": "bottom", "product_id": 9}],
        quantity=2,
    )
    metres, total = _summary([req], mock_session)
    assert metres == {9: 2.2, 7: 2.2}  # (700 + 400) x 2 each
    assert total == pytest.approx(2.2 * 3.0 + 2.2 * 1.0)


def test_an_auto_tape_whose_every_side_went_special_bills_nothing(mock_session):
    req = _req(
        {"sides": ["left"], "product_id": 7}, [{"side": "left", "product_id": 9}]
    )
    metres, _ = _summary([req], mock_session)
    assert metres == {9: 0.7}


# --------------------------------------------------------------------------- #
# Placed piece
# --------------------------------------------------------------------------- #
def test_placed_edges_keep_the_old_shape_without_special_edges(mock_session):
    svc = OptimizationService(mock_session)
    req = _req(_AUTO)
    edges = svc._geometric_edges(req.edge_banding, _TAPES, False)
    assert list(edges) == [
        "sides",
        "nominal_sides",
        "product_id",
        "code",
        "color",
        "band_type",
        "alias",
        "notation",
    ]


def test_placed_edges_rotate_the_special_edges_with_the_piece(mock_session):
    svc = OptimizationService(mock_session)
    req = _req(_AUTO, [{"side": "bottom", "product_id": 9}])
    edges = svc._geometric_edges(req.edge_banding, _TAPES, True, req.special_edges)

    # Clockwise: left→top, right→bottom, top→right, bottom→left.
    assert edges["sides"] == ["top", "bottom", "left", "right"]
    assert edges["nominal_sides"] == ["top", "bottom", "left", "right"]
    assert edges["product_id"] == 7 and edges["band_type"] == "Soft"
    assert edges["notation"] == "2L1C CS CSH · 1C CD BLN"
    [special] = edges["special"]
    assert (special["side"], special["nominal_side"]) == ("left", "bottom")
    assert (special["product_id"], special["band_type"]) == (9, "Hard")


def test_an_auto_tape_with_no_side_left_is_not_named(mock_session):
    svc = OptimizationService(mock_session)
    req = _req(
        {"sides": ["left"], "product_id": 7}, [{"side": "left", "product_id": 9}]
    )
    edges = svc._geometric_edges(req.edge_banding, _TAPES, False, req.special_edges)
    assert edges["product_id"] is None and edges["band_type"] is None
    assert edges["sides"] == ["left"]
    assert edges["notation"] == "1L CD BLN"


def test_the_diagram_reads_the_band_type_per_side():
    edges = {
        "sides": ["top", "left"],
        "band_type": "Soft",
        "special": [{"side": "left", "band_type": "Hard"}],
    }
    assert _side_band_types(edges) == {"top": "Soft", "left": "Hard"}
    assert _side_band_types({}) == {}


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #
def test_the_order_piece_freezes_the_special_edges_next_to_the_auto_banding():
    auto = {"sides": ["top"], "product_id": 7, "band_type": "Soft", "alias": "CSH"}
    special = [{"side": "left", "product_id": 9, "band_type": "Hard", "alias": "BLN"}]

    assert _piece_edges({"edge_banding": auto}) == auto
    assert _piece_edges({"edge_banding": None}) is None
    assert _piece_edges({"edge_banding": auto, "special_edges": special}) == {
        **auto,
        "special_edges": special,
    }
    # Only special edges: the auto keys are still there, empty.
    assert _piece_edges({"edge_banding": None, "special_edges": special}) == {
        "sides": [],
        "product_id": None,
        "band_type": None,
        "alias": None,
        "special_edges": special,
    }


# --------------------------------------------------------------------------- #
# Order document
# --------------------------------------------------------------------------- #
def test_the_cantos_column_wraps_between_tapes_never_inside_one():
    from src.modules.optimizations.documents import _edge_banding_notation

    auto = {"sides": ["left", "top"], "band_type": "Soft", "alias": "CSH"}
    special = [{"side": "left", "band_type": "Hard", "alias": "BLN"}]
    assert _edge_banding_notation({"edge_banding": auto}) == "1L1C CS CSH"
    assert (
        _edge_banding_notation({"edge_banding": auto, "special_edges": special})
        == "1C\u00a0CS\u00a0CSH · 1L\u00a0CD\u00a0BLN"
    )
    assert _edge_banding_notation({"edge_banding": None}) == "-"


# --------------------------------------------------------------------------- #
# Drawings: one line per tape
# --------------------------------------------------------------------------- #
def _labels_drawn(monkeypatch, notation, rect=(0, 0, 900, 300)):
    """The text lines ``_draw_piece_labels`` pastes for a piece, in order."""
    from PIL import Image

    from src.modules.optimizations import visualization as viz

    drawn = []
    real = viz._text_image

    def spy(text, font, color):
        drawn.append(text)
        return real(text, font, color)

    img = Image.new("RGBA", (rect[2], rect[3]), "white")
    font = viz._load_font(21)
    piece = {"piece_id": "Lateral#1", "height": 720, "width": 400}
    piece["edges"] = {"notation": notation}
    # The dimension labels are drawn first; count only what follows them.
    monkeypatch.setattr(viz, "_text_image", spy)
    viz.VisualizationService._draw_piece_labels(img, rect, piece, font, font)
    return drawn[2:]


def test_the_diagram_draws_each_tape_on_its_own_line(monkeypatch):
    lines = _labels_drawn(monkeypatch, "2L1C CS CSH · 1C CS BNL")
    assert lines == ["Lateral", "2L1C CS CSH", "1C CS BNL"]


def test_a_single_tape_still_draws_one_notation_line(monkeypatch):
    assert _labels_drawn(monkeypatch, "2L1C CS CSH") == ["Lateral", "2L1C CS CSH"]


def test_the_label_prints_every_special_tape_on_its_own_line(monkeypatch):
    from src.modules.print_jobs import label

    printed = []
    real_truncate = label._truncate
    monkeypatch.setattr(
        label,
        "_truncate",
        lambda draw, text, font, width: printed.append(text)
        or real_truncate(draw, text, font, width),
    )
    data = label.LabelData(
        order_code="ORD-000042",
        client_name="Juan Pérez",
        piece_label="Lateral",
        width_mm=400,
        height_mm=720,
        notation="2L1C CS CSH · 1C CS BNL",
        sides={"top", "bottom", "left", "right"},
    )
    label._render_raster(data)
    assert printed[-2:] == ["Lateral  2L1C CS CSH", "1C CS BNL"]
