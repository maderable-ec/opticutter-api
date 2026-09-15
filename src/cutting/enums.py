from enum import Enum


class SplitRule(Enum):
    """Rules for splitting leftover rectangles after placing a piece"""

    SHORTER_LEFTOVER_AXIS = "shorter_leftover_axis"
    LONGER_LEFTOVER_AXIS = "longer_leftover_axis"
    MINIMIZE_AREA = "minimize_area"
    MAXIMIZE_AREA = "maximize_area"
    SHORTER_AXIS = "shorter_axis"
    LONGER_AXIS = "longer_axis"


class Selection(str, Enum):
    """How the packer picks WHICH free rectangle a piece goes into.

    ``BEST_AREA_FIT`` (the default everywhere) ranks gaps by the leftover area
    after placing the piece: the tightest gap wins, which minimizes total waste
    but fragments it across several offcuts.

    ``BOTTOM_LEFT`` ranks by position first — the gap furthest left and down
    wins, ties broken by area fit — which pushes pieces into a corner and leaves
    the dominant leftover as one continuous strip on the opposite side.

    This is an axis of the search's candidate portfolio (see
    ``constructors.GREEDY_PORTFOLIO``), not a user-facing setting: the seller
    picks no heuristic, the search evaluates both and keeps whatever bills less.
    The integer codes the Rust kernel reads are in ``rust_backend``; they are the
    FFI contract and must keep matching ``rust/src/models.rs``.
    """

    BEST_AREA_FIT = "best_area_fit"
    BOTTOM_LEFT = "bottom_left"
