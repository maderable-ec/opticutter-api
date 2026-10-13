"""Which reasons an activity may pause for, and when a wait lifts by itself
(``orders/pauses.py``), without a database.

The case behind it is order 232: every piece cut the day before, and the banding
paused for lack of boards.
"""

from types import SimpleNamespace

from src.modules.orders.model import (
    PAUSE_REASONS_BY_ACTIVITY,
    ActivityType,
    PauseReason,
)
from src.modules.orders.pauses import (
    banding_is_open,
    owes_pieces,
    pause_reasons,
    piece_arrives,
)

CUT, BAND, EXTRA = (
    ActivityType.cutting,
    ActivityType.banding,
    ActivityType.additional,
)
BOARDS, TAPE, MACHINE, WAITING = (
    PauseReason.no_boards,
    PauseReason.no_edge_banding,
    PauseReason.machine_failure,
    PauseReason.waiting_for_pieces,
)


def _row(kind: ActivityType, status: str) -> SimpleNamespace:
    return SimpleNamespace(type=kind.value, status=status)


# ------------------------------------------------------------------ the table
def test_every_activity_has_its_own_reasons():
    assert PAUSE_REASONS_BY_ACTIVITY == {
        CUT: (BOARDS, MACHINE),
        BAND: (WAITING, TAPE, MACHINE),
        EXTRA: (WAITING, MACHINE),
    }


def test_a_machine_can_fail_anywhere_and_only_the_saw_runs_out_of_boards():
    for reasons in PAUSE_REASONS_BY_ACTIVITY.values():
        assert MACHINE in reasons
    assert [a for a, r in PAUSE_REASONS_BY_ACTIVITY.items() if BOARDS in r] == [CUT]
    assert [a for a, r in PAUSE_REASONS_BY_ACTIVITY.items() if TAPE in r] == [BAND]


# ------------------------------------------------------------ what is owed
def test_the_cut_never_waits_for_pieces():
    """Nothing is upstream of the saw, whatever is still to cut."""
    assert not owes_pieces(CUT, pending=40, banding_open=True)
    assert pause_reasons(CUT, 40, True) == [BOARDS, MACHINE]


def test_the_bander_is_owed_pieces_only_while_banded_ones_are_on_the_saw():
    assert owes_pieces(BAND, pending=3, banding_open=True)
    assert pause_reasons(BAND, 3, True) == [WAITING, TAPE, MACHINE]


def test_order_232_every_banded_piece_cut_leaves_tape_and_machine():
    """Its OWN banding being open owes the bander nothing: they are the banding."""
    assert not owes_pieces(BAND, pending=0, banding_open=True)
    assert pause_reasons(BAND, 0, True) == [TAPE, MACHINE]


def test_the_additional_work_waits_on_the_saw_and_on_the_banding():
    assert owes_pieces(EXTRA, pending=2, banding_open=False)
    # Everything cut, but the pieces are still at the edge bander.
    assert owes_pieces(EXTRA, pending=0, banding_open=True)
    assert pause_reasons(EXTRA, 0, True) == [WAITING, MACHINE]


def test_the_additional_work_is_owed_nothing_once_both_are_through():
    assert not owes_pieces(EXTRA, pending=0, banding_open=False)
    assert pause_reasons(EXTRA, 0, False) == [MACHINE]


# --------------------------------------------------------- when a piece arrives
def test_a_piece_outside_the_set_reaches_nobody():
    for activity in (BAND, EXTRA):
        assert not piece_arrives(
            activity, in_set=False, banded=False, banding_open=True
        )


def test_a_banded_piece_reaches_the_bander_the_moment_it_is_cut():
    assert piece_arrives(BAND, in_set=True, banded=True, banding_open=True)


def test_a_worked_piece_with_no_banding_goes_straight_to_the_additional_work():
    assert piece_arrives(EXTRA, in_set=True, banded=False, banding_open=True)


def test_a_worked_piece_that_is_banded_goes_to_the_edge_bander_first():
    assert not piece_arrives(EXTRA, in_set=True, banded=True, banding_open=True)
    # With the banding closed -- or not applicable -- nothing stands in between.
    assert piece_arrives(EXTRA, in_set=True, banded=True, banding_open=False)


# ---------------------------------------------------------- the order's banding
def test_the_banding_is_open_until_it_closes():
    cut = _row(CUT, "in_progress")
    assert banding_is_open([cut, _row(BAND, "pending")])
    assert banding_is_open([cut, _row(BAND, "in_progress")])
    assert not banding_is_open([cut, _row(BAND, "done")])


def test_an_order_without_banding_has_none_open():
    assert not banding_is_open([_row(CUT, "in_progress"), _row(EXTRA, "pending")])
    assert not banding_is_open([])
