"""Which reasons an activity may pause for, and when a pause lifts by itself.

An activity pauses for what can actually stop IT (``PAUSE_REASONS_BY_ACTIVITY``).
The case that started this: order 232 had every piece cut the day before, and its
banding was paused for lack of BOARDS. What the bander really runs into is the
station before not handing over more pieces, and that is its own reason:
``waiting_for_pieces``.

It is the one reason the system can check, so it is held to its cause at both
ends. It is only accepted while the activity is still OWED pieces
(``owes_pieces``), and it ends the instant one ARRIVES (``piece_arrives``, or
the banding closing) -- a pause cannot outlive what it waits for. That matters
more here than anywhere: the banding marks no pieces, so nothing else tells a
bander waiting from a bander working.

Pure: counts and flags in, answers out. The service reads the pieces.
"""

from typing import Iterable

from src.modules.orders.model import (
    PAUSE_REASONS_BY_ACTIVITY,
    ActivityStatus,
    ActivityType,
    PauseReason,
)

# The activities whose pieces may cross the edge bander before reaching them: a
# piece is banded first and hinged, grooved or assembled after. The banding
# marks no pieces, so all they can know about it is whether it has closed.
WAITS_ON_BANDING = frozenset({ActivityType.additional})


def banding_is_open(activities: Iterable) -> bool:
    """Whether the order carries a banding that has not closed yet.

    ``activities``: the order's rows (``type``/``status``). No banding row is
    "does not apply", which is closed for this purpose: nothing will come from it.
    """
    return any(
        a.type == ActivityType.banding.value and a.status != ActivityStatus.done.value
        for a in activities
    )


def owes_pieces(activity_type: ActivityType, pending: int, banding_open: bool) -> bool:
    """Whether the stations before still owe this activity pieces.

    ``pending``: the pieces of the activity's OWN set (``ACTIVITY_PIECES``) not
    cut yet. With none pending the saw owes it nothing -- and then only an open
    banding can, to the activities that work after it.
    """
    if PauseReason.waiting_for_pieces not in PAUSE_REASONS_BY_ACTIVITY[activity_type]:
        return False
    if pending > 0:
        return True
    return banding_open and activity_type in WAITS_ON_BANDING


def pause_reasons(
    activity_type: ActivityType, pending: int, banding_open: bool
) -> list[PauseReason]:
    """The reasons this activity may pause for RIGHT NOW, in the dialog's order.

    Its row of the table, less ``waiting_for_pieces`` once nothing is owed.
    It is what the pause endpoint accepts and what the activity's response
    offers, so the web never mirrors the table.
    """
    owed = owes_pieces(activity_type, pending, banding_open)
    return [
        reason
        for reason in PAUSE_REASONS_BY_ACTIVITY[activity_type]
        if reason is not PauseReason.waiting_for_pieces or owed
    ]


def piece_arrives(
    activity_type: ActivityType, *, in_set: bool, banded: bool, banding_open: bool
) -> bool:
    """Whether a piece just cut reaches this activity, lifting its wait.

    ``in_set``: the piece belongs to the activity's own set. ``banded``: it
    carries edge banding. A banded piece goes to the edge bander FIRST, so for
    an activity that works after the banding it has not arrived yet: it arrives
    when the banding closes, which is the caller's other trigger.
    """
    if not in_set:
        return False
    return not (banded and banding_open and activity_type in WAITS_ON_BANDING)
