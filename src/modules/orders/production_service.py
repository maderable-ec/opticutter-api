"""Loads the shop floor's cutting events and finished sheets, and the live state.

The rules live in ``production.py`` (pure); this is the only place that reads
them off the tables, so ``/analytics`` and the live state count the same rows.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    ActivityStatus,
    ActivityType,
    OrderActivityModel,
    OrderBoardModel,
    OrderModel,
    OrderPlacedPieceModel,
    OrderStatus,
)
from src.modules.orders.pieces_export import customer_name
from src.modules.orders.production import (
    LiveState,
    activity_live_state,
    board_weight,
    cut_length_m,
    live_state,
    run_start,
)
from src.modules.orders.schemas import (
    ActivityLiveStatus,
    BranchProductionStatus,
    ProductionStatusReport,
)
from src.shared.config import config
from src.shared.database import get_db

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
        for branch_id, code, order_id in (
            self.db.query(OrderModel.branch_id, OrderModel.code, OrderModel.id)
            .join(OrderActivityModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderModel.status == OrderStatus.in_process.value,
                OrderActivityModel.type == ActivityType.cutting.value,
                OrderActivityModel.status == ActivityStatus.in_progress.value,
            )
            .order_by(OrderActivityModel.started_at, OrderModel.id)
            .all()
        ):
            cutting_codes[branch_id].append(code or f"#{order_id}")

        tracks = self._activity_tracks([b.id for b in branches])

        items = []
        for branch in branches:
            last = last_ever.get(branch.id)
            has_work = bool(queued.get(branch.id)) or bool(cutting_codes[branch.id])
            state = live_state(last, now, gap, has_work)
            since = (
                run_start(recent[branch.id], gap)
                if state is LiveState.cutting
                else last
            )
            items.append(
                BranchProductionStatus(
                    branch_id=branch.id,
                    branch_name=branch.name,
                    state=state,
                    since=since,
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
        ready: dict[tuple, list[datetime]] = defaultdict(list)
        for branch_id, code, order_id, kind, status, started_at, ready_at in (
            self.db.query(
                OrderModel.branch_id,
                OrderModel.code,
                OrderModel.id,
                OrderActivityModel.type,
                OrderActivityModel.status,
                OrderActivityModel.started_at,
                OrderActivityModel.ready_at,
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
            if status == ActivityStatus.in_progress.value:
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
            state = activity_live_state(len(runs), len(waits))
            if runs:
                since = runs[0][0]
            elif waits:
                since = min(waits)
            else:
                since = last_closed.get(key)
            return ActivityLiveStatus(
                state=state,
                since=since,
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
