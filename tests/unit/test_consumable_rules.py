"""The rules of a consumable change (``consumables/rules.py``), without a database.

Instants are naive UTC; the shop floor is Ecuador's (UTC-5). October 2026: the
5th is a Monday, the 9th a Friday, the 10th a Saturday. Default schedule:
08:00-13:00 and 14:00-18:00 on weekdays, 08:00-13:00 on Saturday.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.modules.consumables.rules import (
    Gate,
    average,
    can_undo,
    change_gate,
    lives,
    meters_per_kg,
    usage_by_life,
    worked_seconds,
)
from src.shared.work_schedule import DEFAULT_WORK_SCHEDULE, WorkSchedule

_SCHEDULE = WorkSchedule.from_dict(DEFAULT_WORK_SCHEDULE)


def _at(day, hour, minute=0, second=0):
    """A wall-clock time on the shop floor, as the naive UTC it is stored in."""
    return datetime(2026, 10, day, hour, minute, second) + timedelta(hours=5)


# ------------------------------------------------------------------- the gate
def test_the_first_change_of_a_consumable_is_free():
    assert change_gate(None, _at(5, 9), _SCHEDULE) is Gate.free


@pytest.mark.parametrize("seconds_later", [0, 20, 60])
def test_a_change_within_a_minute_of_the_last_is_blocked(seconds_later):
    last = _at(5, 9)
    now = last + timedelta(seconds=seconds_later)
    assert change_gate(last, now, _SCHEDULE) is Gate.blocked


def test_past_the_minute_and_under_four_hours_it_is_early():
    last = _at(5, 9)
    assert change_gate(last, last + timedelta(seconds=61), _SCHEDULE) is Gate.early
    assert change_gate(last, _at(5, 12, 59), _SCHEDULE) is Gate.early


def test_four_hours_of_work_later_it_is_free():
    # 10:00 to 15:00 is five on the clock and four of work: lunch is not use.
    assert change_gate(_at(5, 10), _at(5, 14, 59), _SCHEDULE) is Gate.early
    assert change_gate(_at(5, 10), _at(5, 15), _SCHEDULE) is Gate.free


def test_a_night_between_two_changes_is_not_four_hours_of_use():
    """Mounted at 17:30 and changed at 08:30: fifteen hours on the clock, one of work."""
    last, now = _at(5, 17, 30), _at(6, 8, 30)
    assert worked_seconds(last, now, _SCHEDULE) == 3600
    assert change_gate(last, now, _SCHEDULE) is Gate.early


def test_a_weekend_between_two_changes_is_not_use_either():
    # Friday 17:00 to Monday 09:00: an hour on Friday, Saturday's five, Monday's one.
    assert worked_seconds(_at(9, 17), _at(12, 9), _SCHEDULE) == 7 * 3600
    assert change_gate(_at(9, 17), _at(12, 9), _SCHEDULE) is Gate.free
    # Saturday 12:00 to Monday 09:00 is two.
    assert change_gate(_at(10, 12), _at(12, 9), _SCHEDULE) is Gate.early


# ------------------------------------------------------------------- the undo
def test_the_author_undoes_their_latest_change_for_a_few_minutes():
    registered = _at(5, 9)
    assert can_undo(registered, 7, 7, True, registered + timedelta(minutes=10))
    assert not can_undo(registered, 7, 7, True, registered + timedelta(minutes=11))


def test_nobody_undoes_somebody_else_s_change_nor_one_already_replaced():
    registered, now = _at(5, 9), _at(5, 9, 1)
    assert not can_undo(registered, 7, 8, True, now)
    assert not can_undo(registered, 7, 7, False, now)
    # A change with no user (deleted since) is nobody's to undo.
    assert not can_undo(registered, None, None, True, now)


# ------------------------------------------------------------------ the lives
@dataclass
class _Change:
    id: int
    changed_at: datetime


def test_each_change_opens_a_life_that_the_next_one_closes():
    first, second, third = (
        _Change(1, _at(5, 9)),
        _Change(2, _at(7, 10)),
        _Change(3, _at(9, 11)),
    )

    # Out of order on purpose: the lives come back oldest first.
    result = lives([third, first, second])

    assert [(life.opening.id, life.start, life.end) for life in result] == [
        (1, _at(5, 9), _at(7, 10)),
        (2, _at(7, 10), _at(9, 11)),
        (3, _at(9, 11), None),
    ]
    assert [life.closing.id if life.closing else None for life in result] == [
        2,
        3,
        None,
    ]


def test_no_change_no_life():
    assert lives([]) == []


def test_work_is_dealt_to_the_life_it_falls_in():
    starts = [_at(5, 9), _at(7, 10)]
    events = [
        (_at(5, 8), 14.0, 1.0),  # before the first change: nobody's
        (_at(5, 10), 14.0, 1.0),
        (_at(6, 15), 16.0, 0.5),
        (_at(7, 10), 15.0, 1.0),  # on the change itself: the new blade's
        (_at(8, 9), 20.0, 1.0),
    ]

    first, second = usage_by_life(starts, events)

    assert (first.meters, first.units) == (30.0, 1.5)
    assert (second.meters, second.units) == (35.0, 2.0)


def test_a_life_with_no_work_registered_reads_zero():
    [usage] = usage_by_life([_at(5, 9)], [])
    assert (usage.meters, usage.units) == (0.0, 0.0)


# ------------------------------------------------------------------ the yield
def test_average_of_nothing_is_no_figure():
    assert average([]) is None
    assert average([1000.0, 1400.0]) == 1200.0


def test_the_glue_s_yield_is_read_over_all_the_loads_together():
    """The pot is topped up, not emptied: 900 m on 5 kg and 1300 m on 5 kg are
    one glue at 220 m/kg, not one at 180 and one at 260."""
    assert meters_per_kg([(900.0, 5.0), (1300.0, 5.0)]) == 220.0
    # A load with no quantity on record cannot weigh in.
    assert meters_per_kg([(900.0, 5.0), (400.0, None)]) == 180.0
    assert meters_per_kg([]) is None
