"""The shop's working hours, and what they do to a stretch of time.

The shop floor registers events (an order taken, a piece marked, an activity
closed), never the hours in between. A stretch between two events is work --
unless it ran across the night, the lunch hour or the weekend, where nobody was
at the machine. The schedule is what tells the two apart, so an order left half
cut at 17:55 and resumed at 08:20 counts 25 minutes and not 14 hours.

Stored on the settings row as ``{"mon": [["08:00", "13:00"], ...], ...}``: one
list of windows per weekday, on the business's wall clock. ``business_time`` is
still the only place that converts; this module only asks it for a day's
midnight.

Pure: no database, naive UTC in and out like every stored timestamp.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import AbstractSet, Optional

from src.shared.business_time import local_date, local_midnight_utc

# Monday first, like ``date.weekday()``.
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# The shop's hours as business gave them (2026-10-12): Monday to Friday 08:00 to
# 18:00 with lunch from 13:00 to 14:00, Saturday 08:00 to 13:00.
_WEEKDAY = [["08:00", "13:00"], ["14:00", "18:00"]]
DEFAULT_WORK_SCHEDULE: dict[str, list[list[str]]] = {
    "mon": _WEEKDAY,
    "tue": _WEEKDAY,
    "wed": _WEEKDAY,
    "thu": _WEEKDAY,
    "fri": _WEEKDAY,
    "sat": [["08:00", "13:00"]],
    "sun": [],
}

# More windows than this in one day is a typo, not a shift pattern.
MAX_WINDOWS_PER_DAY = 3

# How far apart two events can be and still be continuous work outside working
# hours, and how far past closing a stretch may run. Somebody who stays late
# leaves events minutes apart; a lone mark at 22:58 after an afternoon of
# silence is pieces being registered from home (cutter_db, order 31 on
# 2026-09-15: it read as a 13.5-hour day of sawing).
OVERTIME_LIMIT = timedelta(hours=1)

Interval = tuple[datetime, datetime]


def _minutes(value: str) -> int:
    """``"08:30"`` as minutes after midnight; ``"24:00"`` closes a day."""
    hours, _, minutes = str(value).partition(":")
    total = int(hours) * 60 + int(minutes)
    if not (0 <= int(minutes) < 60 and 0 <= total <= 24 * 60):
        raise ValueError(f"Hora inválida: {value}")
    return total


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_schedule(raw: dict) -> tuple[tuple[tuple[int, int], ...], ...]:
    """The stored shape as minutes per weekday. Raises ``ValueError`` on a bad one.

    Windows of a day come back in order; they must not be empty, nor overlap.
    A missing day has no windows (the shop is closed).
    """
    if not isinstance(raw, dict):
        raise ValueError("El horario debe ser un objeto por día")
    days = []
    for key in DAY_KEYS:
        windows = sorted(
            (_minutes(start), _minutes(end)) for start, end in (raw.get(key) or [])
        )
        if len(windows) > MAX_WINDOWS_PER_DAY:
            raise ValueError(f"Máximo {MAX_WINDOWS_PER_DAY} franjas por día")
        for i, (start, end) in enumerate(windows):
            if end <= start:
                raise ValueError("Cada franja debe terminar después de empezar")
            if i and start < windows[i - 1][1]:
                raise ValueError("Las franjas de un día no pueden solaparse")
        days.append(tuple(windows))
    return tuple(days)


@dataclass(frozen=True)
class WorkSchedule:
    """Working windows per weekday (Monday = 0), in minutes after midnight."""

    days: tuple[tuple[tuple[int, int], ...], ...]

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "WorkSchedule":
        """Reads a stored schedule, falling back to the default on a bad one.

        Coerces instead of raising: this runs on every report, and a row
        written before the column existed (or damaged since) must not turn
        the statistics into a 500.
        """
        try:
            return cls(parse_schedule(raw))
        except (ValueError, TypeError, AttributeError):
            return cls(parse_schedule(DEFAULT_WORK_SCHEDULE))

    def to_dict(self) -> dict[str, list[list[str]]]:
        return {
            key: [[_clock(start), _clock(end)] for start, end in windows]
            for key, windows in zip(DAY_KEYS, self.days)
        }

    # ----------------------------------------------------------------- windows
    def windows(
        self,
        start: datetime,
        end: datetime,
        open_days: Optional[AbstractSet[date]] = None,
    ) -> list[Interval]:
        """The working windows around ``[start, end]``, whole, oldest first.

        ``open_days``: the days somebody actually worked. A scheduled day
        missing from it is a day the shop was closed (a holiday) and yields no
        window; ``None`` takes the schedule at its word.
        """
        day = local_date(start) - timedelta(days=1)
        last = local_date(end) + timedelta(days=1)
        out: list[Interval] = []
        while day <= last:
            if open_days is None or day in open_days:
                midnight = local_midnight_utc(day)
                out += [
                    (
                        midnight + timedelta(minutes=opens),
                        midnight + timedelta(minutes=closes),
                    )
                    for opens, closes in self.days[day.weekday()]
                ]
            day += timedelta(days=1)
        return out

    def overlap(
        self,
        start: datetime,
        end: datetime,
        open_days: Optional[AbstractSet[date]] = None,
    ) -> list[Interval]:
        """The part of ``[start, end]`` that falls inside working hours."""
        if end <= start:
            return []
        return [
            (max(start, opens), min(end, closes))
            for opens, closes in self.windows(start, end, open_days)
            if closes > start and opens < end
        ]

    def worked(
        self,
        start: datetime,
        end: datetime,
        open_days: Optional[AbstractSet[date]] = None,
    ) -> list[Interval]:
        """The part of the stretch between two events that counts as work.

        ``end`` is an event somebody registered, so they were working right up
        to it; ``start`` only says they were working until then. From that:

        - two events up to ``OVERTIME_LIMIT`` apart are continuous work,
          whatever the clock says: pieces marked at 18:05 and 18:40, or at
          07:40 and 08:10, are somebody at the saw;
        - a longer stretch counts its working hours only. The night, the lunch
          hour and the weekend it ran across are dropped, and so is the time
          off it STARTS in -- a piece marked at 18:20 and the next one at 08:30
          is half an hour, not fourteen;
        - except the time off it ENDS in, while that stays within the limit:
          the event at its end is the evidence that they stayed late. Past
          the limit it is a late registration, not somebody who kept working.
        """
        if end <= start:
            return []
        if end - start <= OVERTIME_LIMIT:
            return [(start, end)]
        windows = self.windows(start, end, open_days)
        pieces = [
            (max(start, opens), min(end, closes))
            for opens, closes in windows
            if closes > start and opens < end
        ]
        in_hours = any(opens <= end < closes for opens, closes in windows)
        if pieces and not in_hours and end - pieces[-1][1] <= OVERTIME_LIMIT:
            pieces[-1] = (pieces[-1][0], end)
        return pieces


def seconds(intervals: list[Interval]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)
