"""Loads the shop floor's cutting events and finished sheets, and the live state.

The rules live in ``production.py`` (pure); this is the only place that reads
them off the tables, so ``/analytics`` and the live state count the same rows.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from fastapi import Depends
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    ActivityStatus,
    ActivityType,
    OrderActivityModel,
    OrderActivityPauseModel,
    OrderBoardModel,
    OrderModel,
    OrderPlacedPieceModel,
    OrderStatus,
)
from src.modules.orders.pieces_export import customer_name
from src.modules.orders.production import (
    ActivityLiveState,
    EventKind,
    LiveState,
    TimelineEvent,
    WorkSegment,
    activity_live_state,
    board_weight,
    cut_length_m,
    cut_segments,
    live_state,
    run_start,
)
from src.modules.orders.schemas import (
    ActivityLiveStatus,
    BranchProductionStatus,
    ProductionStatusReport,
)
from src.modules.settings.service import SettingsService
from src.shared.business_time import local_date
from src.shared.config import config
from src.shared.database import get_db
from src.shared.work_schedule import WorkSchedule

# How far back the live state looks for the run in progress. A run longer than
# a day is not a run; the latest event itself is read without a window.
_LIVE_WINDOW = timedelta(days=1)

# The work that marks no pieces: its live state is read off its activity rows.
_ACTIVITY_TRACKS = (ActivityType.banding, ActivityType.additional)


def idle_gap() -> timedelta:
    """The gap that makes a stop (``config.PRODUCTION_IDLE_MINUTES``)."""
    return timedelta(minutes=config.PRODUCTION_IDLE_MINUTES)


@dataclass(frozen=True)
class CutEvent:
    """One touch of the saw's work: a piece marked, a cut taken or closed."""

    branch_id: int
    order_id: int
    at: datetime
    user_id: Optional[int]
    # "piece" | "start" | "finish": a piece counts as cut work, the other two
    # only frame the run (taking the order before its first mark is work too).
    kind: str


@dataclass(frozen=True)
class PauseRecord:
    """One registered pause of an activity, with what a report says of it."""

    id: int
    activity_id: int
    order_id: int
    branch_id: int
    activity: str  # ``ActivityType`` value
    reason: str  # ``PauseReason`` value
    note: Optional[str]
    started_at: datetime
    started_by: Optional[int]
    started_by_label: Optional[str]
    # Null: the pause in force.
    ended_at: Optional[datetime]
    ended_by_label: Optional[str]

    def interval(self, now: datetime) -> tuple[datetime, datetime]:
        """Its span so far: a pause in force runs up to ``now``."""
        return self.started_at, self.ended_at or now


@dataclass(frozen=True)
class OrderCut:
    """One order's cut as the statistics read it: its work, segment by segment.

    ``taken_at`` is the cutting activity's start (null on an order cut before
    activities had clocks, or sent back to the queue); the marks bound the cut.
    """

    order_id: int
    branch_id: int
    taken_at: Optional[datetime]
    first_mark_at: datetime
    last_mark_at: datetime
    segments: tuple[WorkSegment, ...]
    pauses: tuple[PauseRecord, ...]


@dataclass(frozen=True)
class ProcessedBoard:
    """A sheet with every piece cut, dated by its last piece."""

    board_id: int
    order_id: int
    branch_id: int
    done_at: datetime
    weight: float
    cut_linear_m: float
    # Who marked the last piece: the operator the sheet is credited to.
    cut_by: Optional[int]


@dataclass(frozen=True)
class BandingClose:
    """A banding closed, with the net tape its order carried."""

    order_id: int
    branch_id: int
    finished_at: datetime
    banded_m: float


@dataclass(frozen=True)
class OperatorSheet:
    """A sheet one operator marked pieces on, with everything the count reads.

    ``done_at`` (the last mark) and ``closed_by`` (its author) are null while
    some piece is unmarked.
    """

    board_id: int
    order_id: int
    order_code: Optional[str]
    client_name: str
    branch_id: int
    sheet_number: int
    material_name: Optional[str]
    width: float
    height: float
    half_board: bool
    source: Optional[str]
    weight: float
    pieces_total: int
    pieces_mine: int
    pieces_mine_in_range: int
    pieces_pending: int
    other_cutters: tuple[str, ...]
    my_last_cut_at: Optional[datetime]
    done_at: Optional[datetime]
    closed_by: Optional[int]
    closed_by_label: Optional[str]


class ProductionService:
    def __init__(self, db: Session):
        self.db = db
        self._schedule: Optional[WorkSchedule] = None

    @property
    def schedule(self) -> WorkSchedule:
        """The shop's working hours (settings), read once per request."""
        if self._schedule is None:
            self._schedule = SettingsService(self.db).get_work_schedule()
        return self._schedule

    # ---------------------------------------------------------------- the cut
    def open_days(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> dict[int, set[date]]:
        """``branch_id -> business days`` with a cutting event in ``[start, end)``.

        The days the shop was actually open, which is what tells a holiday from
        a working day nobody registered anything on (``WorkSchedule.windows``).
        Marks are read by the hour: a day needs one, not seven thousand.
        """
        days: dict[int, set[date]] = defaultdict(set)
        marked = (
            self.db.query(
                OrderModel.branch_id,
                func.date_trunc("hour", OrderPlacedPieceModel.cut_at),
            )
            .join(OrderModel, OrderPlacedPieceModel.order_id == OrderModel.id)
            .filter(
                OrderPlacedPieceModel.cut_at >= start,
                OrderPlacedPieceModel.cut_at < end,
            )
            .distinct()
        )
        taken = (
            self.db.query(OrderModel.branch_id, OrderActivityModel.started_at)
            .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderActivityModel.type == ActivityType.cutting.value,
                OrderActivityModel.started_at >= start,
                OrderActivityModel.started_at < end,
            )
        )
        for query in (marked, taken):
            if branch_id is not None:
                query = query.filter(OrderModel.branch_id == branch_id)
            for bid, at in query.all():
                days[bid].add(local_date(at))
        return days

    def order_cuts(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> tuple[list[OrderCut], dict[int, set[date]]]:
        """The cut of every order that was being cut at some point of ``[start, end)``.

        Each comes with its WHOLE timeline, not just the events in range: the
        stretch that reaches into the range can start before it (an order taken
        on the 30th and finished on the 1st). The caller clips to the range
        (``work_by_day``). Also returns the branches' open days over the span
        those timelines cover.
        """
        marks = (
            self.db.query(
                OrderPlacedPieceModel.order_id.label("order_id"),
                func.min(OrderPlacedPieceModel.cut_at).label("first_mark"),
                func.max(OrderPlacedPieceModel.cut_at).label("last_mark"),
            )
            .filter(OrderPlacedPieceModel.cut_at.isnot(None))
            .group_by(OrderPlacedPieceModel.order_id)
            .subquery()
        )
        query = (
            self.db.query(
                OrderModel.id,
                OrderModel.branch_id,
                OrderActivityModel.id,
                OrderActivityModel.started_at,
                OrderActivityModel.started_by,
                marks.c.first_mark,
                marks.c.last_mark,
            )
            .join(marks, marks.c.order_id == OrderModel.id)
            .outerjoin(
                OrderActivityModel,
                and_(
                    OrderActivityModel.order_id == OrderModel.id,
                    OrderActivityModel.type == ActivityType.cutting.value,
                ),
            )
            .filter(
                marks.c.last_mark >= start,
                func.least(
                    marks.c.first_mark,
                    func.coalesce(OrderActivityModel.started_at, marks.c.first_mark),
                )
                < end,
            )
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        rows = query.all()
        if not rows:
            return [], {}

        order_ids = [row[0] for row in rows]
        events: dict[int, list[TimelineEvent]] = defaultdict(list)
        for order_id, at, by in self.db.query(
            OrderPlacedPieceModel.order_id,
            OrderPlacedPieceModel.cut_at,
            OrderPlacedPieceModel.cut_by,
        ).filter(
            OrderPlacedPieceModel.order_id.in_(order_ids),
            OrderPlacedPieceModel.cut_at.isnot(None),
        ):
            events[order_id].append(TimelineEvent(at, EventKind.mark, by))
        pauses = self.pauses_of([row[2] for row in rows if row[2] is not None])
        for order_id, _, activity_id, taken_at, taken_by, _, _ in rows:
            if taken_at is not None:
                events[order_id].append(
                    TimelineEvent(taken_at, EventKind.take, taken_by)
                )
            for pause in pauses.get(activity_id, ()):
                events[order_id].append(
                    TimelineEvent(pause.started_at, EventKind.pause, pause.started_by)
                )
                if pause.ended_at is not None:
                    events[order_id].append(
                        TimelineEvent(pause.ended_at, EventKind.resume)
                    )

        earliest = min(event.at for evs in events.values() for event in evs)
        open_days = self.open_days(min(earliest, start), end, branch_id)
        # A timeline's own events open its days, whatever the range held.
        for order_id, bid, *_ in rows:
            open_days.setdefault(bid, set()).update(
                local_date(event.at) for event in events[order_id]
            )

        cuts = [
            OrderCut(
                order_id=order_id,
                branch_id=bid,
                taken_at=taken_at,
                first_mark_at=first_mark,
                last_mark_at=last_mark,
                segments=tuple(
                    cut_segments(events[order_id], self.schedule, open_days[bid])
                ),
                pauses=tuple(pauses.get(activity_id, ())),
            )
            for order_id, bid, activity_id, taken_at, _, first_mark, last_mark in rows
        ]
        return cuts, open_days

    # ------------------------------------------------------------------ pauses
    def _pause_query(self):
        return (
            self.db.query(
                OrderActivityPauseModel,
                OrderActivityModel.type,
                OrderModel.branch_id,
            )
            .join(
                OrderActivityModel,
                OrderActivityPauseModel.activity_id == OrderActivityModel.id,
            )
            .join(OrderModel, OrderActivityPauseModel.order_id == OrderModel.id)
        )

    @staticmethod
    def _pause_records(rows: Iterable) -> list[PauseRecord]:
        return [
            PauseRecord(
                id=pause.id,
                activity_id=pause.activity_id,
                order_id=pause.order_id,
                branch_id=branch_id,
                activity=kind,
                reason=pause.reason,
                note=pause.note,
                started_at=pause.started_at,
                started_by=pause.started_by,
                started_by_label=pause.started_by_label,
                ended_at=pause.ended_at,
                ended_by_label=pause.ended_by_label,
            )
            for pause, kind, branch_id in rows
        ]

    def pauses(
        self,
        start: datetime,
        end: datetime,
        branch_id: Optional[int] = None,
        activity: Optional[ActivityType] = None,
    ) -> list[PauseRecord]:
        """Every pause that overlaps ``[start, end)``, oldest first.

        Overlaps, not "started in": a pause registered on Friday and lifted on
        Monday stopped Monday's work too. One still in force overlaps every
        range from its start on.
        """
        query = self._pause_query().filter(
            OrderActivityPauseModel.started_at < end,
            or_(
                OrderActivityPauseModel.ended_at.is_(None),
                OrderActivityPauseModel.ended_at >= start,
            ),
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        if activity is not None:
            query = query.filter(OrderActivityModel.type == activity.value)
        return self._pause_records(
            query.order_by(
                OrderActivityPauseModel.started_at, OrderActivityPauseModel.id
            ).all()
        )

    def pauses_of(self, activity_ids: Iterable[int]) -> dict[int, list[PauseRecord]]:
        """``activity_id -> its pauses``, oldest first."""
        ids = list(activity_ids)
        if not ids:
            return {}
        by_activity: dict[int, list[PauseRecord]] = defaultdict(list)
        rows = (
            self._pause_query()
            .filter(OrderActivityPauseModel.activity_id.in_(ids))
            .order_by(OrderActivityPauseModel.started_at, OrderActivityPauseModel.id)
            .all()
        )
        for record in self._pause_records(rows):
            by_activity[record.activity_id].append(record)
        return by_activity

    # ------------------------------------------------------------------ events
    def cut_events(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> list[CutEvent]:
        """Every cutting event in ``[start, end)``, unordered.

        Three sources: the pieces marked (``cut_at``, by ``cut_by``) and the
        cutting activity's start and close. The branch is the order's, which is
        frozen once the shop starts on it.
        """
        events: list[CutEvent] = []
        pieces = (
            self.db.query(
                OrderModel.branch_id,
                OrderPlacedPieceModel.order_id,
                OrderPlacedPieceModel.cut_at,
                OrderPlacedPieceModel.cut_by,
            )
            .join(OrderModel, OrderPlacedPieceModel.order_id == OrderModel.id)
            .filter(
                OrderPlacedPieceModel.cut_at >= start,
                OrderPlacedPieceModel.cut_at < end,
            )
        )
        if branch_id is not None:
            pieces = pieces.filter(OrderModel.branch_id == branch_id)
        events += [
            CutEvent(branch_id=b, order_id=o, at=at, user_id=u, kind="piece")
            for b, o, at, u in pieces.all()
        ]
        for kind, at_col, by_col in (
            ("start", OrderActivityModel.started_at, OrderActivityModel.started_by),
            ("finish", OrderActivityModel.finished_at, OrderActivityModel.finished_by),
        ):
            query = (
                self.db.query(
                    OrderModel.branch_id, OrderActivityModel.order_id, at_col, by_col
                )
                .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
                .filter(
                    OrderActivityModel.type == ActivityType.cutting.value,
                    at_col >= start,
                    at_col < end,
                )
            )
            if branch_id is not None:
                query = query.filter(OrderModel.branch_id == branch_id)
            events += [
                CutEvent(branch_id=b, order_id=o, at=at, user_id=u, kind=kind)
                for b, o, at, u in query.all()
            ]
        return events

    # ------------------------------------------------------------------ sheets
    def processed_boards(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> list[ProcessedBoard]:
        """Sheets whose LAST piece was cut in ``[start, end)``.

        A sheet is processed when every one of its pieces is marked, and it is
        dated by the latest mark: a sheet started yesterday and finished today
        is today's. Unmarking a piece takes it back out.
        """
        touched = (
            self.db.query(OrderPlacedPieceModel.board_id)
            .filter(
                OrderPlacedPieceModel.cut_at >= start,
                OrderPlacedPieceModel.cut_at < end,
            )
            .distinct()
            .subquery()
        )
        rows = (
            self.db.query(
                OrderPlacedPieceModel.board_id,
                func.count(OrderPlacedPieceModel.id),
                func.count(OrderPlacedPieceModel.cut_at),
                func.max(OrderPlacedPieceModel.cut_at),
            )
            .filter(OrderPlacedPieceModel.board_id.in_(touched.select()))
            .group_by(OrderPlacedPieceModel.board_id)
            .all()
        )
        done_at = {
            board_id: last
            for board_id, total, cut, last in rows
            if total == cut and start <= last < end
        }
        if not done_at:
            return []

        boards_q = (
            self.db.query(
                OrderBoardModel.id,
                OrderBoardModel.order_id,
                OrderBoardModel.half_board,
                OrderBoardModel.material_key,
                OrderBoardModel.cuts,
                OrderModel.branch_id,
            )
            .join(OrderModel, OrderBoardModel.order_id == OrderModel.id)
            .filter(OrderBoardModel.id.in_(list(done_at)))
        )
        if branch_id is not None:
            boards_q = boards_q.filter(OrderModel.branch_id == branch_id)
        boards = boards_q.all()
        if not boards:
            return []

        sources = self._material_sources({b.order_id for b in boards})
        last_cutter = self._last_cutters(done_at)
        return [
            ProcessedBoard(
                board_id=b.id,
                order_id=b.order_id,
                branch_id=b.branch_id,
                done_at=done_at[b.id],
                weight=board_weight(
                    b.half_board, sources.get((b.order_id, b.material_key))
                ),
                cut_linear_m=cut_length_m(b.cuts),
                cut_by=last_cutter.get(b.id),
            )
            for b in boards
        ]

    def operator_sheets(
        self,
        user_id: int,
        start: datetime,
        end: datetime,
        branch_id: Optional[int] = None,
    ) -> list[OperatorSheet]:
        """Every sheet ``user_id`` marked a piece on in ``[start, end)``.

        The ledger behind an operator's board count: the sheets they closed in
        range are exactly the ones ``processed_boards`` credits them (same
        completion, same last mark, same weight), and the rest say why not --
        closed by somebody else, still missing a piece, or closed outside the
        range.
        """
        touched = self.db.query(OrderPlacedPieceModel.board_id).filter(
            OrderPlacedPieceModel.cut_by == user_id,
            OrderPlacedPieceModel.cut_at >= start,
            OrderPlacedPieceModel.cut_at < end,
        )
        if branch_id is not None:
            touched = touched.join(
                OrderModel, OrderPlacedPieceModel.order_id == OrderModel.id
            ).filter(OrderModel.branch_id == branch_id)
        board_ids = {board_id for (board_id,) in touched.distinct().all()}
        if not board_ids:
            return []

        pieces: dict[int, list] = defaultdict(list)
        for row in self.db.query(
            OrderPlacedPieceModel.board_id,
            OrderPlacedPieceModel.cut_at,
            OrderPlacedPieceModel.cut_by,
            OrderPlacedPieceModel.cut_by_label,
        ).filter(OrderPlacedPieceModel.board_id.in_(list(board_ids))):
            pieces[row.board_id].append(row)
        done_at = {
            board_id: max(p.cut_at for p in rows)
            for board_id, rows in pieces.items()
            if all(p.cut_at is not None for p in rows)
        }
        closers = self._last_cutters(done_at) if done_at else {}
        labels = {
            (p.board_id, p.cut_at): p.cut_by_label
            for rows in pieces.values()
            for p in rows
            if p.cut_at is not None
        }

        boards = (
            self.db.query(OrderBoardModel, OrderModel.code, OrderModel.branch_id)
            .join(OrderModel, OrderBoardModel.order_id == OrderModel.id)
            .filter(OrderBoardModel.id.in_(list(board_ids)))
            .all()
        )
        order_ids = {board.order_id for board, _, _ in boards}
        sources = self._material_sources(order_ids)
        clients = {
            order_id: customer_name(client)
            for order_id, client in self.db.query(OrderModel.id, ClientModel)
            .join(ClientModel, OrderModel.client_id == ClientModel.id)
            .filter(OrderModel.id.in_(list(order_ids)))
        }

        sheets = []
        for board, code, board_branch in boards:
            rows = pieces[board.id]
            mine = [p for p in rows if p.cut_by == user_id and p.cut_at is not None]
            source = sources.get((board.order_id, board.material_key))
            last = done_at.get(board.id)
            sheets.append(
                OperatorSheet(
                    board_id=board.id,
                    order_id=board.order_id,
                    order_code=code,
                    client_name=clients.get(board.order_id, ""),
                    branch_id=board_branch,
                    sheet_number=board.sheet_number,
                    material_name=board.product_name,
                    width=board.width,
                    height=board.height,
                    half_board=board.half_board,
                    source=source,
                    weight=board_weight(board.half_board, source),
                    pieces_total=len(rows),
                    pieces_mine=len(mine),
                    pieces_mine_in_range=sum(
                        1 for p in mine if start <= p.cut_at < end
                    ),
                    pieces_pending=sum(1 for p in rows if p.cut_at is None),
                    other_cutters=tuple(
                        sorted(
                            {
                                p.cut_by_label or ""
                                for p in rows
                                if p.cut_at is not None and p.cut_by != user_id
                            }
                        )
                    ),
                    my_last_cut_at=max(p.cut_at for p in mine),
                    done_at=last,
                    closed_by=closers.get(board.id) if last else None,
                    closed_by_label=labels.get((board.id, last)) if last else None,
                )
            )
        return sheets

    # ----------------------------------------------------------------- banding
    def banded_meters_of(self, order_ids: Iterable[int]) -> dict[int, float]:
        """``order_id -> net metres of tape`` its banding applied.

        ``total_edge_banding_linear_m`` of the frozen snapshot: the edges of
        every placed piece, special edges included -- what the bander actually
        laid. The billed metres carry the waste factor on top and would count
        tape nobody applied. Only that key of the snapshot is read.
        """
        ids = list(order_ids)
        if not ids:
            return {}
        meters: dict[int, float] = {}
        for order_id, value in (
            self.db.query(
                OrderModel.id,
                OrderModel.optimization_snapshot["total_edge_banding_linear_m"],
            )
            .filter(OrderModel.id.in_(ids))
            .all()
        ):
            try:
                meters[order_id] = float(value or 0.0)
            except (TypeError, ValueError):
                meters[order_id] = 0.0
        return meters

    def banding_closes(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> list[BandingClose]:
        """Every banding closed in ``[start, end)``, dated by its closing.

        The banding marks no pieces, so its close is the only instant its
        metres can be placed at -- the one the statistics use too
        (``PerformanceService._closed_activities``).
        """
        query = (
            self.db.query(
                OrderActivityModel.order_id,
                OrderModel.branch_id,
                OrderActivityModel.finished_at,
            )
            .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderActivityModel.type == ActivityType.banding.value,
                OrderActivityModel.finished_at >= start,
                OrderActivityModel.finished_at < end,
            )
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        rows = query.all()
        meters = self.banded_meters_of({order_id for order_id, _, _ in rows})
        return [
            BandingClose(
                order_id=order_id,
                branch_id=bid,
                finished_at=finished_at,
                banded_m=meters.get(order_id, 0.0),
            )
            for order_id, bid, finished_at in rows
        ]

    def _material_sources(self, order_ids: set[int]) -> dict[tuple[int, str], str]:
        """``(order_id, material_key) -> source`` off the frozen snapshots.

        Reads only the ``materials`` subtree: the snapshot carries every layout
        and would cost a lot more to ship whole.
        """
        rows = (
            self.db.query(OrderModel.id, OrderModel.optimization_snapshot["materials"])
            .filter(OrderModel.id.in_(list(order_ids)))
            .all()
        )
        sources: dict[tuple[int, str], str] = {}
        for order_id, materials in rows:
            for material in materials if isinstance(materials, list) else []:
                if isinstance(material, dict) and material.get("material_key"):
                    sources[(order_id, material["material_key"])] = material.get(
                        "source"
                    )
        return sources

    def _last_cutters(self, done_at: dict[int, datetime]) -> dict[int, Optional[int]]:
        """``board_id -> cut_by`` of the piece that closed the sheet."""
        rows = (
            self.db.query(
                OrderPlacedPieceModel.board_id,
                OrderPlacedPieceModel.cut_at,
                OrderPlacedPieceModel.cut_by,
            )
            .filter(OrderPlacedPieceModel.board_id.in_(list(done_at)))
            .filter(OrderPlacedPieceModel.cut_at.isnot(None))
            .all()
        )
        best: dict[int, tuple[datetime, Optional[int]]] = {}
        for board_id, at, by in rows:
            if board_id not in best or at > best[board_id][0]:
                best[board_id] = (at, by)
        return {board_id: by for board_id, (_, by) in best.items()}

    # -------------------------------------------------------------- live state
    def status(
        self, branch_ids: Optional[list[int]] = None, now: Optional[datetime] = None
    ) -> ProductionStatusReport:
        """Live state of the active branches (or of ``branch_ids``)."""
        now = now or datetime.utcnow()
        gap = idle_gap()
        branches_q = self.db.query(BranchModel).filter(BranchModel.is_active.is_(True))
        if branch_ids is not None:
            branches_q = branches_q.filter(BranchModel.id.in_(branch_ids))
        branches = branches_q.order_by(BranchModel.id).all()

        recent: dict[int, list[datetime]] = defaultdict(list)
        for event in self.cut_events(now - _LIVE_WINDOW, now + timedelta(seconds=1)):
            recent[event.branch_id].append(event.at)
        last_ever = self._last_event_per_branch()

        queued = dict(
            self.db.query(OrderModel.branch_id, func.count(OrderModel.id))
            .filter(OrderModel.status == OrderStatus.queued.value)
            .group_by(OrderModel.branch_id)
            .all()
        )
        cutting_codes: dict[int, list[str]] = defaultdict(list)
        # ``(paused_at, order code, reason)`` of the cuts registered as paused.
        paused_cuts: dict[int, list[tuple]] = defaultdict(list)
        for branch_id, code, order_id, paused_at, reason in (
            self.db.query(
                OrderModel.branch_id,
                OrderModel.code,
                OrderModel.id,
                OrderActivityModel.paused_at,
                OrderActivityModel.pause_reason,
            )
            .join(OrderActivityModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderModel.status == OrderStatus.in_process.value,
                OrderActivityModel.type == ActivityType.cutting.value,
                OrderActivityModel.status == ActivityStatus.in_progress.value,
            )
            .order_by(OrderActivityModel.started_at, OrderModel.id)
            .all()
        ):
            if paused_at is not None:
                paused_cuts[branch_id].append(
                    (paused_at, code or f"#{order_id}", reason)
                )
            else:
                cutting_codes[branch_id].append(code or f"#{order_id}")

        tracks = self._activity_tracks([b.id for b in branches])

        items = []
        for branch in branches:
            last = last_ever.get(branch.id)
            paused = sorted(paused_cuts[branch.id])
            has_work = (
                bool(queued.get(branch.id))
                or bool(cutting_codes[branch.id])
                or bool(paused)
            )
            # A paused cut explains a quiet saw only when it is the only cut
            # open: with another one in progress and no marks, somebody still
            # has to look.
            state = live_state(
                last,
                now,
                gap,
                has_work,
                paused=bool(paused) and not cutting_codes[branch.id],
            )
            if state is LiveState.cutting:
                since = run_start(recent[branch.id], gap)
            elif state is LiveState.paused:
                since = paused[0][0]
            else:
                since = last
            items.append(
                BranchProductionStatus(
                    branch_id=branch.id,
                    branch_name=branch.name,
                    state=state,
                    since=since,
                    pause_reason=paused[0][2] if state is LiveState.paused else None,
                    paused_order_codes=[code for _, code, _ in paused],
                    last_event_at=last,
                    queued_count=queued.get(branch.id, 0),
                    cutting_order_codes=cutting_codes[branch.id],
                    banding=tracks[(branch.id, ActivityType.banding)],
                    additional=tracks[(branch.id, ActivityType.additional)],
                )
            )
        return ProductionStatusReport(
            idle_minutes=config.PRODUCTION_IDLE_MINUTES, branches=items
        )

    def _activity_tracks(
        self, branch_ids: list[int]
    ) -> dict[tuple[int, ActivityType], ActivityLiveStatus]:
        """``(branch_id, type) -> live status`` of the banding and additional work.

        Only orders in process carry live activities: the queue has not reached
        the shop and a finished order closed all of them. A pending activity is
        "waiting" once READY (``ready_at``: a piece of its set came off the
        saw); before that there is nothing to work on.
        """
        working: dict[tuple, list] = defaultdict(list)
        paused: dict[tuple, list] = defaultdict(list)
        ready: dict[tuple, list[datetime]] = defaultdict(list)
        for (
            branch_id,
            code,
            order_id,
            kind,
            status,
            started_at,
            ready_at,
            paused_at,
            reason,
        ) in (
            self.db.query(
                OrderModel.branch_id,
                OrderModel.code,
                OrderModel.id,
                OrderActivityModel.type,
                OrderActivityModel.status,
                OrderActivityModel.started_at,
                OrderActivityModel.ready_at,
                OrderActivityModel.paused_at,
                OrderActivityModel.pause_reason,
            )
            .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderModel.status == OrderStatus.in_process.value,
                OrderActivityModel.type.in_([t.value for t in _ACTIVITY_TRACKS]),
                OrderActivityModel.status.in_(
                    [ActivityStatus.pending.value, ActivityStatus.in_progress.value]
                ),
            )
            .all()
        ):
            key = (branch_id, ActivityType(kind))
            if paused_at is not None:
                paused[key].append((paused_at, code or f"#{order_id}", reason))
            elif status == ActivityStatus.in_progress.value:
                working[key].append((started_at, order_id, code or f"#{order_id}"))
            elif ready_at is not None:
                ready[key].append(ready_at)

        last_closed = {
            (branch_id, ActivityType(kind)): at
            for branch_id, kind, at in (
                self.db.query(
                    OrderModel.branch_id,
                    OrderActivityModel.type,
                    func.max(OrderActivityModel.finished_at),
                )
                .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
                .filter(
                    OrderActivityModel.type.in_([t.value for t in _ACTIVITY_TRACKS])
                )
                .group_by(OrderModel.branch_id, OrderActivityModel.type)
                .all()
            )
        }

        def track(key) -> ActivityLiveStatus:
            # A start can be missing on rows written before the activities had
            # clocks; it sorts last instead of breaking the order.
            runs = sorted(
                working[key], key=lambda r: (r[0] is None, r[0] or datetime.min, r[1])
            )
            waits = ready[key]
            stopped = sorted(paused[key])
            state = activity_live_state(len(runs), len(waits), len(stopped))
            if runs:
                since = runs[0][0]
            elif stopped:
                since = stopped[0][0]
            elif waits:
                since = min(waits)
            else:
                since = last_closed.get(key)
            return ActivityLiveStatus(
                state=state,
                since=since,
                pause_reason=(
                    stopped[0][2] if state is ActivityLiveState.paused else None
                ),
                paused_order_codes=[code for _, code, _ in stopped],
                order_codes=[code for _, _, code in runs],
                waiting_count=len(waits),
                last_finished_at=last_closed.get(key),
            )

        return {
            (branch_id, kind): track((branch_id, kind))
            for branch_id in branch_ids
            for kind in _ACTIVITY_TRACKS
        }

    def _last_event_per_branch(self) -> dict[int, datetime]:
        """The latest cutting event of each branch, however old."""
        latest: dict[int, datetime] = {}

        def keep(rows):
            for branch_id, at in rows:
                if at is not None and (
                    branch_id not in latest or at > latest[branch_id]
                ):
                    latest[branch_id] = at

        keep(
            self.db.query(OrderModel.branch_id, func.max(OrderPlacedPieceModel.cut_at))
            .join(OrderModel, OrderPlacedPieceModel.order_id == OrderModel.id)
            .group_by(OrderModel.branch_id)
            .all()
        )
        for column in (OrderActivityModel.started_at, OrderActivityModel.finished_at):
            keep(
                self.db.query(OrderModel.branch_id, func.max(column))
                .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
                .filter(OrderActivityModel.type == ActivityType.cutting.value)
                .group_by(OrderModel.branch_id)
                .all()
            )
        return latest


def production_service(db: Session = Depends(get_db)) -> ProductionService:
    """``ProductionService`` provider for injection into routes."""
    return ProductionService(db)
