"""The rules of a consumable change, pure: when one may be registered, and what
a life gave.

A LIFE is the stretch from one change to the next of the same type in the same
branch: the blade that went on at the first is the one that came off at the
second. Its work is whatever the shop registered inside it -- a sheet counts
on its last piece and a banding on its close, the same instants the production
statistics use, so the two always add up to the same metres.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, Iterable, Optional, Sequence, TypeVar

from src.shared.work_schedule import WorkSchedule, seconds

# A second tap on the same change: two tablets, or a finger that bounced.
MIN_CHANGE_GAP = timedelta(minutes=1)

# Under this much WORK since the last change, another one is either a mistake
# or a blade that failed, and has to say which. At some 200 m of cut a day
# (cutter_db, 2026-09 to 2026-10) four hours are about 100 m, far below what
# any blade gives. Working hours and not the clock: a blade mounted at 17:30
# and changed at 08:30 was used for an hour, not for fifteen.
EARLY_CHANGE = timedelta(hours=4)

# How long whoever registered a change can take it back on their own.
UNDO_WINDOW = timedelta(minutes=10)


class Gate(str, Enum):
    """What registering a change right now would meet."""

    blocked = "blocked"  # one was registered a moment ago
    early = "early"  # allowed, with the reason
    free = "free"


def worked_seconds(start: datetime, end: datetime, schedule: WorkSchedule) -> float:
    """The working time between two instants, by the shop's schedule as it stands."""
    return seconds(schedule.overlap(start, end))


def change_gate(
    last_at: Optional[datetime], now: datetime, schedule: WorkSchedule
) -> Gate:
    """The gate a change at ``now`` meets, given the last one in force.

    Time and not metres on purpose: the metres only move when a sheet's last
    piece is marked, so an operator who marks late would read as a blade that
    cut nothing. They are shown, not judged.
    """
    if last_at is None:
        return Gate.free
    if now - last_at <= MIN_CHANGE_GAP:
        return Gate.blocked
    if worked_seconds(last_at, now, schedule) < EARLY_CHANGE.total_seconds():
        return Gate.early
    return Gate.free


def can_undo(
    registered_at: datetime,
    author_id: Optional[int],
    user_id: Optional[int],
    is_latest: bool,
    now: datetime,
) -> bool:
    """Whether ``user_id`` can take the change back without the admin.

    Their own, still the latest of its kind, and registered within the window.
    ``registered_at`` is when the row was written, not when the change
    happened: one entered for last week was still registered a minute ago.
    """
    if user_id is None or author_id != user_id or not is_latest:
        return False
    return now - registered_at <= UNDO_WINDOW


T = TypeVar("T")


@dataclass(frozen=True)
class Life(Generic[T]):
    """What one change put to work, up to the change that replaced it."""

    opening: T
    start: datetime
    # Null while it is the one in use.
    closing: Optional[T]
    end: Optional[datetime]


def lives(changes: Sequence[T], at=lambda change: change.changed_at) -> list[Life[T]]:
    """The lives of ONE consumable of one branch, oldest first.

    ``changes``: its changes in force, in any order. Two at the same instant
    keep the order they came in.
    """
    ordered = sorted(changes, key=at)
    return [
        Life(
            opening=change,
            start=at(change),
            closing=ordered[index + 1] if index + 1 < len(ordered) else None,
            end=at(ordered[index + 1]) if index + 1 < len(ordered) else None,
        )
        for index, change in enumerate(ordered)
    ]


@dataclass
class Usage:
    """The work registered inside one life."""

    meters: float = 0.0
    # Blades: boards processed (whole 1, half 0.5, offcut 0). Glue: bandings closed.
    units: float = 0.0


def usage_by_life(
    starts: Sequence[datetime], events: Iterable[tuple[datetime, float, float]]
) -> list[Usage]:
    """``events`` (``at, meters, units``) dealt to the life each one falls in.

    ``starts``: the lives' starts, ascending. A life runs from its start up to
    the next one, so an event exactly on a change belongs to the blade that
    just went on. Work from before the first change is nobody's: there is no
    telling which blade did it.
    """
    usage = [Usage() for _ in starts]
    for at, meters, units in events:
        index = bisect_right(starts, at) - 1
        if index >= 0:
            usage[index].meters += meters
            usage[index].units += units
    return usage


def average(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def meters_per_kg(loads: Iterable[tuple[float, Optional[float]]]) -> Optional[float]:
    """Metres banded per kg of glue, over ``(meters, kg)`` of the loads used up.

    Over all of them together and not load by load: the pot is topped up, not
    emptied, so one load reads high and the next one low for the same glue.
    """
    total_m, total_kg = 0.0, 0.0
    for meters, kg in loads:
        if kg:
            total_m += meters
            total_kg += kg
    return total_m / total_kg if total_kg else None
