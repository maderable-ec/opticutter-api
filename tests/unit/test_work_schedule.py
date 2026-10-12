"""The shop's working hours (``shared/work_schedule.py``), without a database.

Instants are naive UTC; the shop floor is Ecuador's (UTC-5). October 2026: the
5th is a Monday, the 9th a Friday, the 10th a Saturday.
"""

from datetime import date, datetime, timedelta

import pytest

from src.shared.work_schedule import (
    DEFAULT_WORK_SCHEDULE,
    WorkSchedule,
    parse_schedule,
    seconds,
)

_SCHEDULE = WorkSchedule.from_dict(DEFAULT_WORK_SCHEDULE)


def _at(day, hour, minute=0):
    """A wall-clock time on the shop floor, as the naive UTC it is stored in."""
    return datetime(2026, 10, day, hour, minute) + timedelta(hours=5)


def _minutes(start, end, open_days=None):
    return seconds(_SCHEDULE.worked(start, end, open_days)) / 60


# ------------------------------------------------------------ inside the hours
def test_a_stretch_inside_working_hours_counts_whole():
    assert _SCHEDULE.worked(_at(5, 9), _at(5, 11)) == [(_at(5, 9), _at(5, 11))]


def test_a_quiet_stretch_inside_working_hours_still_counts():
    """Nothing but a registered pause takes working hours out."""
    assert _minutes(_at(5, 8), _at(5, 12, 30)) == 270


# -------------------------------------------------------------- time off, run across
def test_the_lunch_hour_the_stretch_ran_across_is_dropped():
    pieces = _SCHEDULE.worked(_at(5, 12, 58), _at(5, 14, 10))
    assert pieces == [
        (_at(5, 12, 58), _at(5, 13)),
        (_at(5, 14), _at(5, 14, 10)),
    ]


def test_the_night_the_stretch_ran_across_is_dropped():
    assert _minutes(_at(5, 17, 55), _at(6, 8, 20)) == 25


def test_the_weekend_is_dropped_but_saturday_morning_is_worked():
    # Friday 16:00 to Monday 08:30: two hours, Saturday's five, half an hour.
    assert _minutes(_at(9, 16), _at(12, 8, 30)) == (2 + 5 + 0.5) * 60


def test_a_scheduled_day_nobody_worked_is_a_holiday():
    """Friday the 9th with no event in the branch: closed, like a Sunday."""
    open_days = {date(2026, 10, 8), date(2026, 10, 12)}
    assert _minutes(_at(8, 17), _at(12, 8, 30), open_days) == 60 + 30


# ------------------------------------------------------------------- overtime
def test_events_after_hours_are_overtime_and_count():
    assert _minutes(_at(5, 18, 5), _at(5, 18, 12)) == 7
    assert _minutes(_at(5, 18, 12), _at(5, 18, 50)) == 38


def test_working_through_lunch_counts():
    assert _minutes(_at(5, 13, 10), _at(5, 13, 25)) == 15


def test_starting_before_the_opening_counts_while_the_events_are_close():
    """Taken at 07:40 and a piece at 08:10: half an hour at the saw."""
    assert _minutes(_at(5, 7, 40), _at(5, 8, 10)) == 30
    # Taken at 06:30 and nothing until 08:10: only the working hours.
    assert _minutes(_at(5, 6, 30), _at(5, 8, 10)) == 10


def test_a_stretch_ending_after_hours_counts_up_to_its_event():
    """The mark at 18:40 is the evidence that they stayed."""
    assert _minutes(_at(5, 17, 50), _at(5, 18, 40)) == 50


def test_a_late_mark_after_a_long_silence_is_not_overtime():
    """Pieces registered at 22:58 did not keep the saw running since 14:00."""
    assert _SCHEDULE.worked(_at(5, 14), _at(5, 22, 58)) == [(_at(5, 14), _at(5, 18))]
    assert _SCHEDULE.worked(_at(5, 18, 20), _at(5, 22)) == []
    # Exactly the limit is still somebody who stayed.
    assert _minutes(_at(5, 17), _at(5, 19)) == 120
    assert _minutes(_at(5, 17), _at(5, 19, 1)) == 60


def test_a_stretch_starting_after_hours_counts_from_the_opening():
    """A piece marked at 18:20 and the next at 08:30 is not fourteen hours."""
    assert _SCHEDULE.worked(_at(5, 18, 20), _at(6, 8, 30)) == [
        (_at(6, 8), _at(6, 8, 30))
    ]


def test_a_stretch_starting_at_lunch_counts_from_the_afternoon():
    assert _minutes(_at(5, 13, 10), _at(5, 14, 30)) == 30


def test_an_empty_or_backwards_stretch_is_nothing():
    assert _SCHEDULE.worked(_at(5, 9), _at(5, 9)) == []
    assert _SCHEDULE.worked(_at(5, 10), _at(5, 9)) == []


# -------------------------------------------------------------------- overlap
def test_overlap_keeps_only_the_working_hours():
    """A pause from 16:00 to 10:00 the next day stopped four working hours."""
    pieces = _SCHEDULE.overlap(_at(5, 16), _at(6, 10))
    assert pieces == [(_at(5, 16), _at(5, 18)), (_at(6, 8), _at(6, 10))]


def test_overlap_of_a_stretch_after_hours_is_empty():
    assert _SCHEDULE.overlap(_at(5, 18, 10), _at(5, 18, 40)) == []


# ------------------------------------------------------------------ the shape
def test_the_default_schedule_round_trips():
    assert _SCHEDULE.to_dict() == DEFAULT_WORK_SCHEDULE


def test_a_missing_or_damaged_schedule_reads_as_the_default():
    """It runs on every report: a bad row must coerce, never raise."""
    for raw in (None, "x", {"mon": [["18:00", "08:00"]]}, {"mon": [["a", "b"]]}):
        assert WorkSchedule.from_dict(raw) == _SCHEDULE


def test_a_day_without_windows_is_closed():
    schedule = WorkSchedule.from_dict({"mon": [["08:00", "12:00"]]})
    assert schedule.days[0] == ((480, 720),)
    assert schedule.days[1] == ()


@pytest.mark.parametrize(
    "windows",
    [
        [["13:00", "08:00"]],  # ends before it starts
        [["08:00", "08:00"]],  # empty
        [["08:00", "13:00"], ["12:00", "18:00"]],  # overlap
        [["08:00", "25:00"]],  # not a time
        [["08:00", "09:00"]] * 4,  # too many
    ],
)
def test_a_bad_schedule_is_rejected_on_the_way_in(windows):
    with pytest.raises(ValueError):
        parse_schedule({"mon": windows})
