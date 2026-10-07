"""Production on the shop floor, read off the cutting events.

Nothing on the shop floor registers "production": there is no pause button and
no shift clock. What it does leave is a timestamp every time somebody touches
the saw's work -- a cut taken (the cutting activity's ``started_at``), a piece
marked (``cut_at``), a cut closed (``finished_at``). The shop's rule is to mark
each piece the moment it comes off the saw, so the gaps between those events
ARE the saw's rhythm: one no longer than the idle gap
(``config.PRODUCTION_IDLE_MINUTES``) is work, a longer one is a stop.

Pure on purpose, and the single home of these rules: the live state
(``GET /orders/production-status``) and the statistics (``/analytics``) both
read them, so the board never calls a stretch "Detenida" that the history later
counts as effective time.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Optional, Sequence

from src.shared.business_time import local_date

# Material sources that are not a sheet anybody buys: a retazo the shop or the
# client already owns. Their cuts are real saw work (their metres count), but a
# retazo is not a "tablero", so it weighs nothing in the board count.
OFFCUT_SOURCES = frozenset({"companyOffcut", "clientOffcut"})


def board_weight(half_board: bool, source: Optional[str]) -> float:
    """How much one physical sheet counts as a "tablero procesado".

    A whole board is 1 and a half board 0.5, so boards per hour compare across
    branches; a retazo is 0. ``manual`` (a hand-measured sheet) is a purchased
    sheet and counts like the catalog's.
    """
    if source in OFFCUT_SOURCES:
        return 0.0
    return 0.5 if half_board else 1.0


def cut_length_m(cuts) -> float:
    """Saw travel of one sheet, in metres: the sum of its guillotine cuts.

    The same number the optimization reports as ``cut_linear_m``. Tolerates a
    sheet frozen before ``cuts`` was serialized: the column is JSON, so the
    missing value arrives as ``None`` (JSON null), never as a list.
    """
    if not isinstance(cuts, list):
        return 0.0
    total_mm = sum(
        float(cut.get("length") or 0.0) for cut in cuts if isinstance(cut, dict)
    )
    return total_mm / 1000.0


@dataclass(frozen=True)
class Stop:
    """A gap longer than the idle gap between two cutting events of one day."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class Workday:
    """One business day of a stream of cutting events.

    ``first_at``/``last_at`` are the day's first and last event (naive UTC).
    The span between them splits exactly into effective + paused time.
    """

    day: date
    first_at: datetime
    last_at: datetime
    effective_seconds: float = 0.0
    paused_seconds: float = 0.0
    stops: list[Stop] = field(default_factory=list)


def workdays(times: Iterable[datetime], idle_gap: timedelta) -> list[Workday]:
    """Splits a stream of event instants into business days, oldest first.

    Inside a day, every gap between two consecutive events is either effective
    (no longer than ``idle_gap``) or a stop. A gap across the business midnight
    belongs to no day: the night is not a stop.

    The stream is ONE saw's work (a branch, or one operator): two branches'
    events interleaved would hide each other's stops.
    """
    by_day: dict[date, list[datetime]] = defaultdict(list)
    for at in times:
        by_day[local_date(at)].append(at)
    days = []
    for day in sorted(by_day):
        stamps = sorted(by_day[day])
        workday = Workday(day=day, first_at=stamps[0], last_at=stamps[-1])
        for prev, cur in zip(stamps, stamps[1:]):
            gap = cur - prev
            if gap <= idle_gap:
                workday.effective_seconds += gap.total_seconds()
            else:
                workday.paused_seconds += gap.total_seconds()
                workday.stops.append(Stop(start=prev, end=cur))
        days.append(workday)
    return days


class LiveState(str, Enum):
    """What a branch's saw is doing right now (only the cut is measured, for now)."""

    cutting = "cutting"  # an event within the idle gap
    stopped = "stopped"  # nothing within the gap, with work waiting
    idle = "idle"  # nothing within the gap, and nothing to do


def live_state(
    last_event_at: Optional[datetime],
    now: datetime,
    idle_gap: timedelta,
    has_work: bool,
) -> LiveState:
    """The branch's live state, by the same idle gap as the statistics.

    ``has_work``: an order in the queue or a cut in progress. Without it a quiet
    saw is a quiet day (``idle``), not a problem (``stopped``).
    """
    if last_event_at is not None and now - last_event_at <= idle_gap:
        return LiveState.cutting
    return LiveState.stopped if has_work else LiveState.idle


def run_start(times: Sequence[datetime], idle_gap: timedelta) -> Optional[datetime]:
    """Where the continuous run ending at the latest event began.

    Walks back from the last event while every gap fits in ``idle_gap``: that
    is the "Cortando desde 08:12" of the live state.
    """
    stamps = sorted(times)
    if not stamps:
        return None
    start = stamps[-1]
    for prev in reversed(stamps[:-1]):
        if start - prev > idle_gap:
            break
        start = prev
    return start
