"""Projection of a placed piece's edge banding for the public review.

Pure function, no DB: ``_to_review_edges`` only reshapes the dict the optimizer
attached to the piece.
"""

import pytest

from src.modules.preorders.public_router import (
    _edge_banding_index,
    _to_review_cut_edges,
    _to_review_edges,
)

# The banding summary the projectors join against, keyed by ``product_id`` — the one
# field both piece shapes carry and neither is allowed to forward.
_SUMMARY = {
    72: {"product_name": "TAPACANTO GRIS RATÓN 22X0.45MM", "color": "Gris Ratón"}
}


def _edges(**overrides) -> dict:
    base = {
        "sides": ["top"],
        "nominal_sides": ["left"],
        "product_id": 72,
        "code": "GRT-C045",
        "color": "Gris Ratón",
        "band_type": "Soft",
        "notation": "1L CS",
    }
    base.update(overrides)
    return base


def test_keeps_both_frames_and_drops_the_catalog_ids():
    out = _to_review_edges(_edges(), rotated=True, eb_by_id=_SUMMARY)

    assert out.sides == ["top"]
    assert out.nominal_sides == ["left"]
    assert out.notation == "1L CS"
    assert out.color == "Gris Ratón"
    assert out.band_type == "Soft"
    # Named, not identified: the name is joined in, the id it was joined on is not.
    assert out.product_name == "TAPACANTO GRIS RATÓN 22X0.45MM"
    assert not hasattr(out, "product_id")
    assert not hasattr(out, "code")


def test_no_banding_projects_to_none():
    assert _to_review_edges(None, rotated=False, eb_by_id=_SUMMARY) is None
    assert _to_review_edges({}, rotated=True, eb_by_id=_SUMMARY) is None
    assert _to_review_cut_edges({"edge_banding": None}, _SUMMARY) is None
    assert _to_review_cut_edges({}, _SUMMARY) is None


def test_cut_list_edges_are_nominal_and_named():
    """The cut list never rotates: these sides come from the requirement itself."""
    out = _to_review_cut_edges(
        {
            "edge_banding": {
                "sides": ["left", "top"],
                "product_id": 72,
                "band_type": "Soft",
                "alias": "GRT",
            }
        },
        _SUMMARY,
    )

    assert out.sides == ["left", "top"]
    assert out.notation == "1L1C CS GRT"
    assert out.special == []
    assert out.band_type == "Soft"
    assert out.product_name == "TAPACANTO GRIS RATÓN 22X0.45MM"
    assert out.color == "Gris Ratón"
    # ``alias`` is a shop tag and ``product_id`` a catalog handle; neither is projected.
    for leaked in ("product_id", "code", "alias"):
        assert not hasattr(out, leaked)


def test_an_unpriced_tape_still_projects():
    """Geometry-only banding has no ``product_id``, so the summary has no row for it."""
    out = _to_review_cut_edges(
        {"edge_banding": {"sides": ["left"], "product_id": None}}, _SUMMARY
    )

    assert out.sides == ["left"]
    assert out.product_name is None


def test_the_index_skips_the_row_with_no_product():
    """``_build_edge_bandings_summary`` emits a ``product_id: None`` row for
    geometry-only banding; keying on it would name every unassigned piece after it."""
    index = _edge_banding_index(
        {
            "edge_bandings_summary": [
                {"product_id": None, "product_name": None},
                {"product_id": 72, "product_name": "TAPACANTO GRIS RATÓN 22X0.45MM"},
            ]
        }
    )

    assert set(index) == {72}
    assert _edge_banding_index({}) == {}


@pytest.mark.parametrize(
    "rotated,geometric,expected",
    [
        # CW is top→right→bottom→left→top, so undoing it maps top back to left.
        (True, ["top"], ["left"]),
        (True, ["top", "bottom", "right"], ["left", "right", "top"]),
        (False, ["left"], ["left"]),
        (False, [], []),
    ],
)
def test_nominal_sides_are_recovered_from_a_cached_payload(
    rotated, geometric, expected
):
    """Results live in Redis for days and the hash covers the inputs, not the
    output shape: a quote cached before ``nominal_sides`` existed still has to
    reach the client's diagram in the piece's own frame."""
    stale = _edges(sides=geometric)
    del stale["nominal_sides"]

    out = _to_review_edges(stale, rotated=rotated, eb_by_id=_SUMMARY)

    assert out.sides == geometric
    assert out.nominal_sides == expected


# A second tape the summary knows, for the cantos especiales.
_WITH_SPECIAL = {
    **_SUMMARY,
    90: {"product_name": "TAPACANTO BLANCO 22X2MM", "color": "Blanco"},
}


def test_a_special_edge_takes_its_side_from_the_auto_tape():
    """The cut list names the auto tape on the sides it kept and each canto
    especial on its own side; the notation says which long side went special."""
    out = _to_review_cut_edges(
        {
            "edge_banding": {
                "sides": ["left", "right", "top"],
                "product_id": 72,
                "band_type": "Soft",
                "alias": "GRT",
            },
            "special_edges": [
                {"side": "left", "product_id": 90, "band_type": "Hard", "alias": "BLN"}
            ],
        },
        _WITH_SPECIAL,
    )

    assert out.sides == ["right", "top", "left"]
    assert out.product_name == "TAPACANTO GRIS RATÓN 22X0.45MM"
    assert out.notation == "1L1C CS GRT · 1L CD BLN"
    [special] = out.special
    assert special.side == "left" and special.nominal_side == "left"
    assert special.band_type == "Hard"
    assert special.product_name == "TAPACANTO BLANCO 22X2MM"


def test_a_piece_banded_only_with_special_edges_names_no_auto_tape():
    out = _to_review_cut_edges(
        {
            "edge_banding": {"sides": ["left"], "product_id": 72, "band_type": "Soft"},
            "special_edges": [
                {"side": "left", "product_id": 90, "band_type": "Hard", "alias": "BLN"}
            ],
        },
        _WITH_SPECIAL,
    )

    assert out.sides == ["left"]
    assert out.band_type is None and out.product_name is None
    assert out.notation == "1L CD BLN"


def test_the_diagram_projects_the_special_edges_in_both_frames():
    edges = _edges(
        sides=["top", "right"],
        nominal_sides=["left", "top"],
        notation="1C CS · 1L CD BLN",
        special=[
            {
                "side": "top",
                "nominal_side": "left",
                "product_id": 90,
                "code": "BLN-2",
                "color": "Blanco",
                "band_type": "Hard",
                "alias": "BLN",
            }
        ],
    )
    out = _to_review_edges(edges, rotated=True, eb_by_id=_WITH_SPECIAL)

    [special] = out.special
    assert (special.side, special.nominal_side) == ("top", "left")
    assert special.product_name == "TAPACANTO BLANCO 22X2MM"
    assert out.notation == "1C CS · 1L CD BLN"
