"""Projection of a placed piece's edge banding for the public review.

Pure function, no DB: ``_to_review_edges`` only reshapes the dict the optimizer
attached to the piece.
"""

import pytest

from src.modules.preorders.public_router import _to_review_edges


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
    out = _to_review_edges(_edges(), rotated=True)

    assert out.sides == ["top"]
    assert out.nominal_sides == ["left"]
    assert out.notation == "1L CS"
    assert out.color == "Gris Ratón"
    assert out.band_type == "Soft"
    # The client never sees the seller's catalog handles.
    assert not hasattr(out, "product_id")
    assert not hasattr(out, "code")


def test_no_banding_projects_to_none():
    assert _to_review_edges(None, rotated=False) is None
    assert _to_review_edges({}, rotated=True) is None


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

    out = _to_review_edges(stale, rotated=rotated)

    assert out.sides == geometric
    assert out.nominal_sides == expected
