"""Scoring rules for the corpus benchmark: what a commercial label can mean.

``corpus_labels`` reads the number the shop wrote in a filename.  This module
decides what that number is *evidence of*, which is a separate question and the
one that decides whether a lost job is an engine defect or a billing note.

**The corpus is two populations, not one.**  For every pool there is a count
that requires no packing at all -- the *area floor*, ``area / sheet`` rounded up
to the next half board.  Nobody can bill below it, and with a 4mm blade,
guillotine cuts and grain-locked parts almost nobody reaches it.  Split the
corpus on where the commercial's label falls and the two halves behave like
different programs:

* label **above** the floor -- their number paid for a real cutting decision,
  the same kind ours pays for.  Measured on the 2026 corpus: 156 pools, and we
  win 31 / tie 112 / lose 13 against them, at a better mean efficiency (76.4%
  vs 72.8%).
* label **exactly at** the floor -- 440 pools, of which we reach 318.  The 122
  we do not reach would need a median 93.7% of the *usable* board, p90 99.2%,
  and eleven of them need more than 100%, which is not a plan anyone cut.  The
  cleanest single case is one 900x760 part on a 1220x2440 sheet -- 23% of the
  board -- filed as ``MEDIO``.  A seller charged half a board; an optimizer did
  not decide it.

So a run reports both, and never adds them into one headline: the first number
grades the engine, the second grades our distance to a theoretical bound.

**The two denominators below are deliberately different.**  ``usable_area``
subtracts the trims, because that is physics -- the shop really does dress 10mm
off each edge, so parts have to fit what is left.  ``area_floor`` divides by the
*raw* sheet, because that is the billing hypothesis being tested: a seller
dividing square metres by the size printed on the board knows nothing about the
saw.  Making them agree would collapse the very distinction this module exists
to draw.
"""

import math
from typing import Mapping

# A label needing more of the usable board than this is not a plan we lost to --
# it is a number that was written down.
#
# This is the engine's demonstrated MAXIMUM over the 2026 corpus (0.976), and it
# used to be its p90 (0.920).  The p90 was the wrong statistic and pre-order 3
# proved it: the shop's white pool of
# ``4 BLANCO 2YMEDIO RSITRETTO-17-08-2026.xml`` carries a label implying 0.946,
# which the old cutoff dismissed as dubious -- yet a 4-board plan for it exists
# and is pinned constructively in ``tests/unit/test_cutting_search.py``.  The
# p90 describes what this engine typically achieves; the question here is what
# is *achievable*, and only the maximum speaks to that.  Anything the search
# leaves between the two is engine debt, not a billing note, so the count of
# winnable pools this constant produces is a floor on our debt rather than a
# ceiling on theirs.
PLAUSIBLE_EFFICIENCY = 0.976

PLAN = "plan"
FLOOR = "piso"


def usable_area(sheet: Mapping[str, float], params) -> float:
    """Board area the parts can actually occupy, trims removed."""
    width = sheet["width"] - params.left_trim - params.right_trim
    height = sheet["height"] - params.top_trim - params.bottom_trim
    return max(0.0, width) * max(0.0, height)


def raw_area(sheet: Mapping[str, float]) -> float:
    """Nominal board area -- the number printed on the board, trims included."""
    return sheet["width"] * sheet["height"]


def half_usable_area(sheet: Mapping[str, float], params) -> float:
    """Usable area of the half sibling the business sells (same length, width/2).

    Not half of ``usable_area``: the rip exposes two fresh edges that get
    dressed too, so a half board loses a second pair of trims.
    """
    width = sheet["width"] / 2.0 - params.left_trim - params.right_trim
    height = sheet["height"] - params.top_trim - params.bottom_trim
    return max(0.0, width) * max(0.0, height)


def ceil_half(value: float) -> float:
    """Rounds up to the next half board, the shop's billing granularity."""
    return math.ceil(value * 2 - 1e-9) / 2.0


def area_floor(parts_area: float, sheet: Mapping[str, float]) -> float:
    """Boards the parts need on area alone, against the RAW sheet.

    The lower bound of the billing hypothesis, not of the cutting problem: see
    the module docstring on why this one ignores the trims.
    """
    sheet_area = raw_area(sheet)
    if sheet_area <= 0:
        return 0.0
    return max(0.5, ceil_half(parts_area / sheet_area))


def capacity(boards: float, sheet: Mapping[str, float], params) -> float:
    """Usable area of ``boards`` half-units, as fulls plus at most one half."""
    fulls = int(boards)
    halves = 1 if boards - fulls >= 0.5 else 0
    return fulls * usable_area(sheet, params) + halves * half_usable_area(sheet, params)


def implied_efficiency(
    used_area: float, boards: float, sheet: Mapping[str, float], params
) -> float:
    """Fraction of the usable board a count implies whoever billed it achieved.

    Above 1.0 the count is physically impossible on this sheet; above
    ``PLAUSIBLE_EFFICIENCY`` it is beyond anything this engine has ever
    produced, on any pool of the corpus.
    """
    room = capacity(boards, sheet, params)
    return used_area / room if room > 0 else float("inf")


def classify(theirs: float, parts_area: float, sheet: Mapping[str, float]) -> str:
    """``PLAN`` when the commercial's label cost them a board the area did not
    demand, ``FLOOR`` when it is exactly the area bound."""
    return PLAN if theirs > area_floor(parts_area, sheet) + 1e-9 else FLOOR
