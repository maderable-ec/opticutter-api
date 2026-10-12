"""The shop floor's production rules (``orders/production.py``), without a database.

The instants are naive UTC like every stored timestamp; the business day is
Ecuador's (UTC-5), so 13:00 UTC is 08:00 on the shop floor and 00:30 UTC is
still the previous evening.
"""

from datetime import date, datetime, timedelta

from src.modules.orders.production import (
    ActivityLiveState,
    EventKind,
    LiveState,
    Phase,
    SheetCredit,
    SheetKind,
    TimelineEvent,
    activity_live_state,
    activity_seconds,
    board_weight,
    clocked_seconds,
    cut_length_m,
    cut_segments,
    live_state,
    paused_by_day,
    run_start,
    sheet_credit,
    sheet_kind,
    work_by_day,
)
from src.shared.business_time import (
    local_date,
    local_midnight_utc,
    minute_of_day,
)
from src.shared.work_schedule import DEFAULT_WORK_SCHEDULE, WorkSchedule

_GAP = timedelta(minutes=15)


def _utc(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second)


# ---------------------------------------------------------------- board weight
def test_a_whole_board_counts_one_and_a_half_board_counts_half():
    assert board_weight(False, "catalog") == 1.0
    assert board_weight(True, "catalog") == 0.5


def test_a_retazo_is_no_board_whoever_owns_it():
    assert board_weight(False, "companyOffcut") == 0.0
    assert board_weight(False, "clientOffcut") == 0.0
    assert board_weight(True, "clientOffcut") == 0.0


def test_a_manual_sheet_is_bought_and_counts_like_the_catalog():
    assert board_weight(False, "manual") == 1.0
    # An order whose snapshot lost the material: counted as a board, never dropped.
    assert board_weight(False, None) == 1.0


def test_the_kind_says_what_the_weight_weighs():
    assert sheet_kind(False, "catalog") is SheetKind.whole
    assert sheet_kind(True, "manual") is SheetKind.half
    assert sheet_kind(True, "companyOffcut") is SheetKind.offcut


# ----------------------------------------------------------------- the ledger
_START, _END = _utc(1, 5), _utc(31, 5)


def test_a_sheet_counts_for_who_closed_it_inside_the_range():
    assert sheet_credit(7, 7, _utc(10, 13), _START, _END) is SheetCredit.credited


def test_a_sheet_closed_by_somebody_else_is_theirs():
    closed = sheet_credit(7, 8, _utc(10, 13), _START, _END)
    assert closed is SheetCredit.credited_to_other
    # A deleted user's mark is somebody else's too.
    assert sheet_credit(7, None, _utc(10, 13), _START, _END) is closed


def test_a_sheet_with_a_piece_unmarked_counts_for_nobody():
    assert sheet_credit(7, None, None, _START, _END) is SheetCredit.incomplete


def test_a_sheet_closed_after_the_range_is_out_of_it():
    assert sheet_credit(7, 7, _END, _START, _END) is SheetCredit.outside_range
    assert sheet_credit(7, 7, _START, _START, _END) is SheetCredit.credited


# ------------------------------------------------------------------ cut metres
def test_cut_length_sums_the_saw_travel_in_metres():
    cuts = [
        {"x": 0, "y": 0, "length": 2440.0, "is_horizontal": True},
        {"x": 0, "y": 0, "length": 1220.0, "is_horizontal": False},
    ]
    assert cut_length_m(cuts) == 3.66


def test_a_sheet_frozen_without_cuts_has_no_metres():
    # A JSON column hands back None for JSON null -- never a list.
    assert cut_length_m(None) == 0.0
    assert cut_length_m([]) == 0.0


# ------------------------------------------------------------------ live state
def test_an_event_within_the_idle_gap_is_cutting():
    now = _utc(6, 15, 0)
    last = now - timedelta(minutes=15)
    assert live_state(last, now, _GAP, has_work=False) is LiveState.cutting


def test_a_quiet_saw_with_work_waiting_is_stopped():
    now = _utc(6, 15, 0)
    last = now - timedelta(minutes=16)
    assert live_state(last, now, _GAP, has_work=True) is LiveState.stopped


def test_a_quiet_saw_with_nothing_to_do_is_idle():
    now = _utc(6, 15, 0)
    assert live_state(now - timedelta(hours=2), now, _GAP, False) is LiveState.idle
    assert live_state(None, now, _GAP, has_work=False) is LiveState.idle
    assert live_state(None, now, _GAP, has_work=True) is LiveState.stopped


def test_the_run_starts_after_the_last_stop():
    times = [_utc(6, 12, 0), _utc(6, 13, 0), _utc(6, 13, 10), _utc(6, 13, 20)]
    assert run_start(times, _GAP) == _utc(6, 13, 0)
    assert run_start([], _GAP) is None


# ------------------------------------------------- banding and additional work
def test_an_activity_in_progress_is_being_worked():
    assert activity_live_state(in_progress=1, waiting=3) is ActivityLiveState.working


def test_ready_work_with_nobody_on_it_is_waiting():
    assert activity_live_state(in_progress=0, waiting=2) is ActivityLiveState.waiting


def test_nothing_in_progress_nor_ready_is_idle():
    assert activity_live_state(in_progress=0, waiting=0) is ActivityLiveState.idle


def test_a_start_and_close_a_minute_apart_or_more_is_clocked():
    start = _utc(6, 15, 0)
    assert clocked_seconds(start, start + timedelta(minutes=1)) == 60.0
    assert clocked_seconds(start, start + timedelta(hours=1, minutes=14)) == 4440.0


def test_a_start_and_close_registered_together_is_not_clocked():
    """The bander taps Iniciar and Terminar after the work: 67 % of the bandings."""
    start = _utc(6, 15, 0)
    assert clocked_seconds(start, start + timedelta(seconds=4)) is None
    assert clocked_seconds(start, start + timedelta(seconds=59)) is None


def test_an_activity_without_start_or_close_is_not_clocked():
    assert clocked_seconds(None, _utc(6, 15, 0)) is None
    assert clocked_seconds(_utc(6, 15, 0), None) is None


# ------------------------------------------------------------- business time
def test_business_days_start_at_the_local_midnight():
    assert local_midnight_utc(date(2026, 10, 1)) == datetime(2026, 10, 1, 5, 0)
    assert local_date(datetime(2026, 10, 2, 4, 59)) == date(2026, 10, 1)
    assert local_date(datetime(2026, 10, 2, 5, 0)) == date(2026, 10, 2)
    assert minute_of_day(datetime(2026, 10, 6, 13, 12)) == 8 * 60 + 12


# ------------------------------------------------------------- the cut's time
# The shop floor's wall clock (UTC-5), the week of Monday 2026-10-05.
_SCHEDULE = WorkSchedule.from_dict(DEFAULT_WORK_SCHEDULE)


def _at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute) + timedelta(hours=5)


def _take(at, user=7):
    return TimelineEvent(at, EventKind.take, user)


def _mark(at, user=7):
    return TimelineEvent(at, EventKind.mark, user)


def _pause(at, user=7):
    return TimelineEvent(at, EventKind.pause, user)


def _resume(at, user=7):
    return TimelineEvent(at, EventKind.resume, user)


def _minutes(segments, phase=None):
    return sum(
        (s.end - s.start).total_seconds() / 60
        for s in segments
        if phase is None or s.phase is phase
    )


def test_the_cut_runs_from_taking_the_order_to_its_last_piece():
    """Business's own example: the load is up to the first piece, the rest is sawing."""
    segments = cut_segments(
        [
            _take(_at(5, 8)),
            _mark(_at(5, 8, 20)),
            _mark(_at(5, 8, 26)),
            _mark(_at(5, 9, 30)),
        ],
        _SCHEDULE,
    )
    assert _minutes(segments, Phase.loading) == 20
    assert _minutes(segments, Phase.sawing) == 70


def test_a_long_load_is_work_not_a_stop():
    """The old rule dropped every stretch over 15 minutes -- 1 load in 3."""
    segments = cut_segments([_take(_at(5, 8)), _mark(_at(5, 8, 45))], _SCHEDULE)
    assert _minutes(segments, Phase.loading) == 45


def test_an_order_with_no_piece_marked_has_no_time_yet():
    assert cut_segments([_take(_at(5, 8))], _SCHEDULE) == []
    assert cut_segments([], _SCHEDULE) == []


def test_a_registered_pause_is_out_of_the_cut():
    segments = cut_segments(
        [
            _take(_at(5, 8)),
            _mark(_at(5, 8, 20)),
            _pause(_at(5, 8, 30)),  # sin tableros
            _resume(_at(5, 10)),
            _mark(_at(5, 10, 20)),
        ],
        _SCHEDULE,
    )
    assert _minutes(segments, Phase.loading) == 20
    assert _minutes(segments, Phase.sawing) == 10 + 20


def test_a_pause_before_the_first_piece_is_out_of_the_load():
    segments = cut_segments(
        [
            _take(_at(5, 8)),
            _pause(_at(5, 8, 5)),
            _resume(_at(5, 9)),
            _mark(_at(5, 9, 15)),
        ],
        _SCHEDULE,
    )
    assert _minutes(segments, Phase.loading) == 5 + 15
    assert _minutes(segments, Phase.sawing) == 0


def test_the_cut_ends_at_its_last_piece():
    """A pause, or the tap on Terminar, after the last mark is not cutting."""
    events = [_take(_at(5, 8)), _mark(_at(5, 8, 20))]
    assert _minutes(cut_segments(events + [_pause(_at(5, 9))], _SCHEDULE)) == 20


def test_the_night_and_the_lunch_hour_are_not_cutting():
    segments = cut_segments(
        [
            _take(_at(5, 12, 40)),
            _mark(_at(5, 12, 58)),
            _mark(_at(5, 14, 10)),  # across lunch: 2 + 10
            _mark(_at(5, 17, 55)),
            _mark(_at(6, 8, 20)),  # across the night: 5 + 20
        ],
        _SCHEDULE,
    )
    assert _minutes(segments, Phase.loading) == 18
    assert _minutes(segments, Phase.sawing) == 12 + 225 + 25


def test_an_order_taken_again_after_a_rollback_does_not_count_the_queue():
    """The admin sent it back to the queue: the wait until it was retaken is no cut."""
    segments = cut_segments(
        [_mark(_at(5, 8, 10)), _take(_at(5, 10)), _mark(_at(5, 10, 30))],
        _SCHEDULE,
    )
    assert _minutes(segments, Phase.loading) == 0
    assert _minutes(segments, Phase.sawing) == 30


def test_a_stretch_is_credited_to_who_closes_it():
    segments = cut_segments(
        [_take(_at(5, 8), user=1), _mark(_at(5, 8, 20), user=2)], _SCHEDULE
    )
    assert [s.user_id for s in segments] == [2]


def test_two_orders_open_at_once_do_not_count_the_same_minute_twice():
    first = cut_segments([_take(_at(5, 8)), _mark(_at(5, 9))], _SCHEDULE)
    second = cut_segments(
        [_take(_at(5, 8, 30)), _mark(_at(5, 8, 40)), _mark(_at(5, 9, 30))], _SCHEDULE
    )
    [day] = work_by_day(first + second).values()
    assert day.day == date(2026, 10, 5)
    assert day.work_seconds == 90 * 60
    # 08:40 to 09:30 is sawing on the second order, whatever the first says.
    assert day.sawing_seconds == 50 * 60
    assert day.loading_seconds == 40 * 60


def test_the_work_is_bucketed_by_business_day_and_clipped_to_the_range():
    segments = cut_segments(
        [_take(_at(5, 17)), _mark(_at(5, 17, 30)), _mark(_at(6, 8, 30))], _SCHEDULE
    )
    days = work_by_day(segments)
    assert days[date(2026, 10, 5)].work_seconds == 60 * 60
    assert days[date(2026, 10, 6)].sawing_seconds == 30 * 60
    only_tuesday = work_by_day(segments, start=_at(6, 0), end=_at(7, 0))
    assert list(only_tuesday) == [date(2026, 10, 6)]


# --------------------------------------------------- the banding's own time
def test_a_banding_is_its_start_to_its_close_within_working_hours():
    """Started at 17:00 and closed at 09:00: two hours, not sixteen."""
    assert activity_seconds(_at(5, 17), _at(6, 9), [], _SCHEDULE) == 2 * 3600


def test_a_paused_banding_does_not_count_the_pause():
    pauses = [(_at(5, 9), _at(5, 11))]  # sin canto
    assert activity_seconds(_at(5, 8), _at(5, 12), pauses, _SCHEDULE) == 2 * 3600


def test_a_banding_registered_after_the_work_is_still_not_clocked():
    start = _at(5, 9)
    assert activity_seconds(start, start + timedelta(seconds=20), [], _SCHEDULE) is None
    assert activity_seconds(None, start, [], _SCHEDULE) is None


# ---------------------------------------------------------- registered pauses
def test_a_pause_counts_only_its_working_hours():
    """16:00 to 10:00 the next day stopped the saw four hours, not eighteen."""
    days = paused_by_day([(_at(5, 16), _at(6, 10))], _SCHEDULE)
    assert days == {date(2026, 10, 5): 2 * 3600, date(2026, 10, 6): 2 * 3600}


def test_two_orders_paused_at_once_stopped_one_saw():
    pauses = [(_at(5, 9), _at(5, 11)), (_at(5, 10), _at(5, 12))]
    assert paused_by_day(pauses, _SCHEDULE) == {date(2026, 10, 5): 3 * 3600}


def test_a_quiet_saw_with_a_registered_pause_says_so():
    now = _utc(6, 15, 0)
    quiet = now - timedelta(hours=1)
    assert live_state(quiet, now, _GAP, True, paused=True) is LiveState.paused
    # Marks within the gap win: somebody is cutting another order.
    assert live_state(now, now, _GAP, True, paused=True) is LiveState.cutting


def test_a_paused_banding_is_not_being_worked():
    assert activity_live_state(0, 2, paused=1) is ActivityLiveState.paused
    assert activity_live_state(1, 0, paused=1) is ActivityLiveState.working
