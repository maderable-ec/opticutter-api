"""The shop floor's production rules (``orders/production.py``), without a database.

The instants are naive UTC like every stored timestamp; the business day is
Ecuador's (UTC-5), so 13:00 UTC is 08:00 on the shop floor and 00:30 UTC is
still the previous evening.
"""

from datetime import date, datetime, timedelta

from src.modules.orders.production import (
    ActivityLiveState,
    LiveState,
    SheetCredit,
    SheetKind,
    activity_live_state,
    board_weight,
    clocked_seconds,
    cut_length_m,
    live_state,
    run_start,
    sheet_credit,
    sheet_kind,
    workdays,
)
from src.shared.business_time import (
    local_date,
    local_midnight_utc,
    minute_of_day,
)

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


# --------------------------------------------------------------------- workday
def test_gaps_up_to_the_idle_gap_are_work_and_longer_ones_are_stops():
    """The example the office was given: 08:00 take, 6, 3, 20 and 58 minutes."""
    times = [
        _utc(6, 13, 0),  # 08:00 takes the order
        _utc(6, 13, 6),  # 08:06 piece 1
        _utc(6, 13, 9),  # 08:09 piece 2
        _utc(6, 13, 29),  # 08:29 piece 3 (20 min)
        _utc(6, 14, 27),  # 09:27 piece 4 (58 min)
    ]
    [day] = workdays(times, _GAP)

    assert day.day == date(2026, 10, 6)
    assert day.effective_seconds == 9 * 60
    assert day.paused_seconds == (20 + 58) * 60
    assert [(s.start, s.end) for s in day.stops] == [
        (_utc(6, 13, 9), _utc(6, 13, 29)),
        (_utc(6, 13, 29), _utc(6, 14, 27)),
    ]
    assert day.first_at == _utc(6, 13, 0)
    assert day.last_at == _utc(6, 14, 27)


def test_a_gap_of_exactly_the_idle_gap_is_still_work():
    [day] = workdays([_utc(6, 13, 0), _utc(6, 13, 15)], _GAP)
    assert day.effective_seconds == 15 * 60
    assert day.paused_seconds == 0
    assert day.stops == []


def test_the_order_of_the_events_does_not_matter():
    times = [_utc(6, 13, 9), _utc(6, 13, 0), _utc(6, 13, 6)]
    [day] = workdays(times, _GAP)
    assert day.effective_seconds == 9 * 60


def test_a_late_cut_belongs_to_its_own_evening_not_to_the_next_day():
    """19:30 in Ecuador is 00:30 UTC of the next day: still the same workday."""
    times = [_utc(6, 23, 50), _utc(7, 0, 0), _utc(7, 0, 30)]  # 18:50, 19:00, 19:30
    [day] = workdays(times, _GAP)
    assert day.day == date(2026, 10, 6)
    assert day.effective_seconds == 10 * 60
    assert day.paused_seconds == 30 * 60


def test_the_night_is_not_a_stop():
    times = [_utc(6, 21, 0), _utc(7, 13, 0)]  # 16:00 today, 08:00 tomorrow
    days = workdays(times, _GAP)
    assert [d.day for d in days] == [date(2026, 10, 6), date(2026, 10, 7)]
    assert all(d.paused_seconds == 0 and d.effective_seconds == 0 for d in days)


def test_no_events_no_days():
    assert workdays([], _GAP) == []


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
