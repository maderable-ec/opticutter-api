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
    assert _to_review_cut_edges(None, _SUMMARY) is None
    assert _to_review_cut_edges({}, _SUMMARY) is None


def test_cut_list_edges_are_nominal_and_named():
    """The cut list never rotates: these sides come from the requirement itself."""
    out = _to_review_cut_edges(
        {
            "sides": ["left", "top"],
            "product_id": 72,
            "band_type": "Soft",
            "alias": "GRT",
        },
        _SUMMARY,
    )

    assert out.sides == ["left", "top"]
    assert out.band_type == "Soft"
    assert out.product_name == "TAPACANTO GRIS RATÓN 22X0.45MM"
    assert out.color == "Gris Ratón"
    # ``alias`` is a shop tag and ``product_id`` a catalog handle; neither is projected.
    for leaked in ("product_id", "code", "alias"):
        assert not hasattr(out, leaked)


def test_an_unpriced_tape_still_projects():
    """Geometry-only banding has no ``product_id``, so the summary has no row for it."""
    out = _to_review_cut_edges({"sides": ["left"], "product_id": None}, _SUMMARY)

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
