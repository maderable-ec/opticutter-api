"""Production on the shop floor, read off the events it registers.

Nobody on the shop floor registers "production": there is no shift clock. What
it does leave is a timestamp every time somebody touches the work -- an order
taken (the cutting activity's ``started_at``), a piece marked (``cut_at``), an
activity paused or resumed, a cut closed. The cut's time is read off those:
from taking the order to its last piece, less the registered pauses and the
time off it ran across (``cut_segments``, with ``shared/work_schedule.py``).

Pure on purpose, and the single home of these rules: the statistics
(``/analytics``) and the live state (``GET /orders/production-status``) both
read them. The live state alone still uses the idle gap
(``config.PRODUCTION_IDLE_MINUTES``): it answers "is the saw moving right
now", which a stretch that is still open cannot.

The banding and the additional work mark no pieces: all they leave is their
activity's start and close, and their pauses. Those are read as registered --
an activity in progress IS being worked -- with one guard, ``clocked_seconds``:
a start and a close a few seconds apart are a registration after the fact, not
a duration.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import AbstractSet, Iterable, Optional, Sequence

from src.shared.business_time import local_date, local_midnight_utc
from src.shared.work_schedule import Interval, WorkSchedule

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


class SheetKind(str, Enum):
    """What a sheet is, as the board count weighs it (``board_weight``)."""

    whole = "whole"
    half = "half"
    offcut = "offcut"


def sheet_kind(half_board: bool, source: Optional[str]) -> SheetKind:
    if source in OFFCUT_SOURCES:
        return SheetKind.offcut
    return SheetKind.half if half_board else SheetKind.whole


class SheetCredit(str, Enum):
    """Whether a sheet an operator worked on counts for them in a range, and why not.

    The rule of the board count, sheet by sheet: a sheet counts once every piece
    is marked, on the day of its last mark, for whoever made that mark.
    """

    credited = "credited"  # they closed it, inside the range
    credited_to_other = "credited_to_other"  # somebody else marked the last piece
    incomplete = "incomplete"  # a piece is still unmarked: it counts for nobody
    outside_range = "outside_range"  # they closed it, but outside the range


def sheet_credit(
    user_id: int,
    closed_by: Optional[int],
    done_at: Optional[datetime],
    start: datetime,
    end: datetime,
) -> SheetCredit:
    """Where one sheet stands for ``user_id`` in ``[start, end)``.

    ``done_at``/``closed_by``: the last mark and its author, ``done_at`` null
    while some piece is unmarked. The same criteria as
    ``ProductionService.processed_boards``, so the credited sheets ARE the ones
    the operator's row counts.
    """
    if done_at is None:
        return SheetCredit.incomplete
    if closed_by != user_id:
        return SheetCredit.credited_to_other
    if not (start <= done_at < end):
        return SheetCredit.outside_range
    return SheetCredit.credited


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


# ------------------------------------------------------------------- work time
# The cut's time, as business reads it (2026-10-12): from the operator TAKING
# the order to its LAST piece marked. Inside it, the stretch up to the first
# piece is the LOADING (pulling the sheets off the rack and onto the saw) and
# the rest is the SAWING; the two add up to the cut's work time, which is what
# every rate divides by. A registered pause is out, and so is the time off the
# stretch ran across (``WorkSchedule.worked``). Nothing else is: inside working
# hours a quiet stretch counts, which is what makes registering the pause the
# thing that protects whoever is waiting on boards.


class EventKind(str, Enum):
    """What an instant on an activity's timeline says."""

    take = "take"  # the activity started: for the cut, the order taken
    mark = "mark"  # work registered: a piece marked, or the activity closed
    pause = "pause"
    resume = "resume"


# Two events of one instant, in the order they can only have happened.
_KIND_ORDER = {
    EventKind.take: 0,
    EventKind.resume: 1,
    EventKind.mark: 2,
    EventKind.pause: 3,
}


@dataclass(frozen=True)
class TimelineEvent:
    at: datetime
    kind: EventKind
    user_id: Optional[int] = None


class Phase(str, Enum):
    loading = "loading"  # taken, no piece marked yet
    sawing = "sawing"  # from the first piece to the last


@dataclass(frozen=True)
class WorkSegment:
    """A stretch of an order's cut that counts as work, credited to one user."""

    start: datetime
    end: datetime
    phase: Phase
    # Who registered the event that closes it: the work that led to a mark is
    # the marker's, the same way a sheet is its last marker's.
    user_id: Optional[int]


def _worked_stretches(
    events: Iterable[TimelineEvent],
    schedule: WorkSchedule,
    open_days: Optional[AbstractSet[date]],
):
    """``(start, end, closing event)`` of every stretch of a timeline that counts.

    A stretch counts when it ENDS in work registered: a mark, or the pause that
    interrupts it. One ending at a take or a resume is the order waiting (in
    the queue after a rollback, or paused), and one starting at a pause is the
    pause itself.
    """
    ordered = sorted(events, key=lambda e: (e.at, _KIND_ORDER[e.kind]))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.kind not in (EventKind.mark, EventKind.pause):
            continue
        if prev.kind is EventKind.pause:
            continue
        for start, end in schedule.worked(prev.at, cur.at, open_days):
            yield start, end, cur


def cut_segments(
    events: Iterable[TimelineEvent],
    schedule: WorkSchedule,
    open_days: Optional[AbstractSet[date]] = None,
) -> list[WorkSegment]:
    """The work of one order's cut: its loading and its sawing.

    ``events``: the order taken, every piece marked, every pause and resume.
    The cut ends at its last mark -- whatever comes after (a pause, the tap on
    "Terminar corte") is not cutting. An order with no piece marked yet has no
    time: its loading only exists once the first piece closes it.
    """
    events = list(events)
    marks = [e.at for e in events if e.kind is EventKind.mark]
    if not marks:
        return []
    first_mark, last_mark = min(marks), max(marks)
    return [
        WorkSegment(
            start=start,
            end=end,
            phase=Phase.loading if closing.at <= first_mark else Phase.sawing,
            user_id=closing.user_id,
        )
        for start, end, closing in _worked_stretches(
            (e for e in events if e.at <= last_mark), schedule, open_days
        )
    ]


def activity_seconds(
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    pauses: Iterable[tuple[datetime, Optional[datetime]]],
    schedule: WorkSchedule,
    open_days: Optional[AbstractSet[date]] = None,
) -> Optional[float]:
    """The worked time of an activity that marks no pieces, or ``None``.

    The banding and the additional work leave a start and a close: the time
    between them, less the registered pauses and the time off it ran across.
    ``None`` when it was not clocked (``clocked_seconds``): the guard reads the
    raw span, since a tap after the work is a tap whatever the schedule says.
    """
    if clocked_seconds(started_at, finished_at) is None:
        return None
    events = [
        TimelineEvent(started_at, EventKind.take),
        TimelineEvent(finished_at, EventKind.mark),
    ]
    for paused_at, resumed_at in pauses:
        events.append(TimelineEvent(paused_at, EventKind.pause))
        if resumed_at is not None:
            events.append(TimelineEvent(resumed_at, EventKind.resume))
    return sum(
        (end - start).total_seconds()
        for start, end, _ in _worked_stretches(events, schedule, open_days)
    )


@dataclass
class DayWork:
    """One business day of cut work: the loading, the sawing and their sum."""

    day: date
    loading_seconds: float = 0.0
    sawing_seconds: float = 0.0

    @property
    def work_seconds(self) -> float:
        return self.loading_seconds + self.sawing_seconds


def _split_by_day(start: datetime, end: datetime):
    """``[start, end)`` cut at the business's midnights."""
    while start < end:
        midnight = local_midnight_utc(local_date(start) + timedelta(days=1))
        cut = min(end, midnight)
        yield start, cut
        start = cut


def _union_seconds(intervals: list[Interval]) -> float:
    """How long the intervals cover together: an overlap counts once."""
    total, reach = 0.0, None
    for start, end in sorted(intervals):
        if reach is None or start > reach:
            total += (end - start).total_seconds()
            reach = end
        elif end > reach:
            total += (end - reach).total_seconds()
            reach = end
    return total


def _clip(
    start: datetime,
    end: datetime,
    lower: Optional[datetime],
    upper: Optional[datetime],
) -> Optional[Interval]:
    if lower is not None:
        start = max(start, lower)
    if upper is not None:
        end = min(end, upper)
    return (start, end) if end > start else None


def work_by_day(
    segments: Iterable[WorkSegment],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> dict[date, DayWork]:
    """The segments of ONE saw (a branch, or one operator) as business days.

    A UNION, not a sum: with two orders open at once the same minute would
    otherwise count twice, and no day can hold more work than it has minutes.
    Where a loading and a sawing overlap the minute is sawing. Clipped to
    ``[start, end)``, the report's range.
    """
    by_day: dict[date, dict[Phase, list[Interval]]] = defaultdict(
        lambda: {Phase.loading: [], Phase.sawing: []}
    )
    for segment in segments:
        clipped = _clip(segment.start, segment.end, start, end)
        if clipped is None:
            continue
        for piece in _split_by_day(*clipped):
            by_day[local_date(piece[0])][segment.phase].append(piece)
    days = {}
    for day, phases in by_day.items():
        sawing = _union_seconds(phases[Phase.sawing])
        total = _union_seconds(phases[Phase.sawing] + phases[Phase.loading])
        days[day] = DayWork(
            day=day, loading_seconds=total - sawing, sawing_seconds=sawing
        )
    return days


def paused_by_day(
    pauses: Iterable[Interval],
    schedule: WorkSchedule,
    open_days: Optional[AbstractSet[date]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> dict[date, float]:
    """Seconds of registered pause per business day, inside working hours only.

    A pause registered at 16:00 and lifted at 10:00 the next morning stopped
    the work for four hours, not eighteen. A union, like the work: two orders
    paused at once stopped one saw.
    """
    by_day: dict[date, list[Interval]] = defaultdict(list)
    for paused_at, resumed_at in pauses:
        clipped = _clip(paused_at, resumed_at, start, end)
        if clipped is None:
            continue
        for window in schedule.overlap(*clipped, open_days):
            for piece in _split_by_day(*window):
                by_day[local_date(piece[0])].append(piece)
    return {day: _union_seconds(pieces) for day, pieces in by_day.items()}


class LiveState(str, Enum):
    """What a branch's saw is doing right now, read off its cutting events."""

    cutting = "cutting"  # an event within the idle gap
    paused = "paused"  # nothing within the gap, and a cut registered as paused
    stopped = "stopped"  # nothing within the gap, with work waiting
    idle = "idle"  # nothing within the gap, and nothing to do


def live_state(
    last_event_at: Optional[datetime],
    now: datetime,
    idle_gap: timedelta,
    has_work: bool,
    paused: bool = False,
) -> LiveState:
    """The branch's live state, read off its latest cutting event.

    ``has_work``: an order in the queue or a cut in progress. Without it a quiet
    saw is a quiet day (``idle``), not a problem (``stopped``). ``paused``: some
    cut in progress carries a registered pause -- a quiet saw then has a reason,
    and says it instead of asking somebody to act.
    """
    if last_event_at is not None and now - last_event_at <= idle_gap:
        return LiveState.cutting
    if paused:
        return LiveState.paused
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


class ActivityLiveState(str, Enum):
    """What a branch's banding (or additional work) is doing right now.

    Read off the activity rows, not off events: the bander marks no pieces, so
    the only signal is the start and the close they register.
    """

    working = "working"  # an activity in progress
    paused = "paused"  # none being worked, some registered as paused
    waiting = "waiting"  # none in progress, some ready (a piece of its set cut)
    idle = "idle"  # nothing in progress and nothing ready


def activity_live_state(
    in_progress: int, waiting: int, paused: int = 0
) -> ActivityLiveState:
    """``in_progress``/``waiting``/``paused``: how many activities are in each.

    ``in_progress`` leaves the paused ones out: an activity somebody stopped
    for lack of tape is not being worked. ``waiting`` counts only the
    activities that are READY (``ready_at``): one whose first piece is still on
    the saw has nothing to work on yet.
    """
    if in_progress:
        return ActivityLiveState.working
    if paused:
        return ActivityLiveState.paused
    return ActivityLiveState.waiting if waiting else ActivityLiveState.idle


# A start and a close closer than this were registered together, after the
# work: 102 of 152 bandings and 18 of 21 additional works closed in under a
# minute (cutter_db, 2026-09-07 to 2026-10-05). Their duration measures the
# tap, not the job, so it stays out of every hour and every rate.
MIN_CLOCKED = timedelta(minutes=1)


def clocked_seconds(
    started_at: Optional[datetime], finished_at: Optional[datetime]
) -> Optional[float]:
    """The activity's duration, or ``None`` when it was not clocked.

    Not clocked: no start (closed without one), no close yet, or a span under
    ``MIN_CLOCKED``. The activity still happened and still counts as work done;
    only its time is unknown.
    """
    if started_at is None or finished_at is None:
        return None
    span = finished_at - started_at
    if span < MIN_CLOCKED:
        return None
    return span.total_seconds()
