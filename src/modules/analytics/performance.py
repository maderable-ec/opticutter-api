"""Branch performance: the comparison, the production tab and productivity by role.

Every figure is placed in the period by WHEN IT HAPPENED, not by when its order
was created: a sale by the payment (``queued_at``), a sheet by its last piece
cut, a closed order by its ``finished`` history row, an activity by its close.
The cutting rules (workday, board weight, saw metres) are the shop floor's own,
in ``orders/production.py``, read through ``ProductionService`` -- the same
ones the live state uses.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.analytics.constants import safe_div
from src.modules.analytics.dates import DateRange
from src.modules.analytics.schemas import (
    BanderFigures,
    BanderReport,
    BanderRow,
    BranchComparison,
    BranchFigures,
    BranchMaterial,
    OperatorFigures,
    OperatorReport,
    OperatorRow,
    OrderFigures,
    ProductionDay,
    ProductionFigures,
    ProductionReport,
    ProductionStop,
    RangeInfo,
    SalesFigures,
    SellerFigures,
    SellerReport,
    SellerRow,
)
from src.modules.branches.model import BranchModel
from src.modules.orders.model import (
    ActivityType,
    OrderActivityModel,
    OrderLineModel,
    OrderModel,
    OrderStatus,
    OrderStatusHistoryModel,
)
from src.modules.orders.production import Workday, workdays
from src.modules.orders.production_service import (
    ProcessedBoard,
    ProductionService,
    idle_gap,
)
from src.modules.users.model import UserModel
from src.shared.business_time import local_date, minute_of_day
from src.shared.config import config
from src.shared.database import get_db

# How many stops the production tab lists, longest first.
_STOPS_LIMIT = 20


@dataclass
class _Sales:
    cash: float = 0.0
    credit: float = 0.0
    paid_orders: int = 0

    def add(self, cash, transfer, credit) -> None:
        # Two methods as the office reads them: received (cash + transfer) and owed.
        self.cash += (cash or 0.0) + (transfer or 0.0)
        self.credit += credit or 0.0
        self.paid_orders += 1

    @property
    def total(self) -> float:
        return self.cash + self.credit


@dataclass
class _Production:
    days: list[Workday] = field(default_factory=list)
    boards: float = 0.0
    cut_linear_m: float = 0.0
    banded_linear_m: float = 0.0

    def add_board(self, board: ProcessedBoard) -> None:
        self.boards += board.weight
        self.cut_linear_m += board.cut_linear_m


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


def _rates(boards: float, meters: float, effective_seconds: float) -> tuple:
    hours = effective_seconds / 3600.0
    return round(safe_div(boards, hours), 2), round(safe_div(meters, hours), 2)


def _production_figures(acc: _Production) -> ProductionFigures:
    """Projects a branch's (or the total's) production accumulator."""
    effective = sum(d.effective_seconds for d in acc.days)
    paused = sum(d.paused_seconds for d in acc.days)
    # A lone event (somebody took an order and nothing else) is not a workday:
    # it would drag the average start to whatever time that happened.
    worked = [d for d in acc.days if d.effective_seconds > 0]
    boards_per_hour, meters_per_hour = _rates(acc.boards, acc.cut_linear_m, effective)
    return ProductionFigures(
        boards=round(acc.boards, 2),
        cut_linear_m=round(acc.cut_linear_m, 2),
        banded_linear_m=round(acc.banded_linear_m, 2),
        effective_hours=_hours(effective),
        paused_hours=_hours(paused),
        boards_per_hour=boards_per_hour,
        meters_per_hour=meters_per_hour,
        # Calendar days: on the total, two branches cutting the same day is one day.
        days_worked=len({d.day for d in worked}),
        average_start_minute=round(
            safe_div(sum(minute_of_day(d.first_at) for d in worked), len(worked))
        ),
        average_end_minute=round(
            safe_div(sum(minute_of_day(d.last_at) for d in worked), len(worked))
        ),
    )


@dataclass(frozen=True)
class _ClosedActivity:
    """A banding or additional work closed in range."""

    order_id: int
    branch_id: int
    kind: str
    started_at: Optional[datetime]
    finished_at: datetime
    finished_by: Optional[int]
    # The order's net tape for a banding; 0 for the additional work.
    banded_m: float


def _sales_figures(acc: _Sales) -> SalesFigures:
    return SalesFigures(
        cash=round(acc.cash, 2),
        credit=round(acc.credit, 2),
        total=round(acc.total, 2),
        paid_orders=acc.paid_orders,
    )


class PerformanceService:
    """Branch comparison, production detail and productivity by role."""

    def __init__(self, db: Session):
        self.db = db
        self.production = ProductionService(db)

    # --------------------------------------------------------------- comparison
    def branch_comparison(self, dr: DateRange) -> BranchComparison:
        sales: dict[int, _Sales] = defaultdict(_Sales)
        for branch_id, _, cash, transfer, credit, _ in self._paid_orders(dr):
            sales[branch_id].add(cash, transfer, credit)

        entered = dict(
            self.db.query(OrderModel.branch_id, func.count(OrderModel.id))
            .filter(OrderModel.created_at >= dr.start, OrderModel.created_at < dr.end)
            .group_by(OrderModel.branch_id)
            .all()
        )
        finished: dict[int, int] = defaultdict(int)
        for branch_id, _, _ in self._finished_orders(dr):
            finished[branch_id] += 1

        production = self._production_by_branch(dr)

        branches = self._branches(
            set(sales) | set(entered) | set(finished) | set(production)
        )
        rows = [
            BranchFigures(
                branch_id=branch.id,
                branch_name=branch.name,
                sales=_sales_figures(sales[branch.id]),
                orders=OrderFigures(
                    entered=entered.get(branch.id, 0), finished=finished[branch.id]
                ),
                production=_production_figures(production[branch.id]),
            )
            for branch in branches
        ]

        total_sales, total_production = _Sales(), _Production()
        for acc in sales.values():
            total_sales.cash += acc.cash
            total_sales.credit += acc.credit
            total_sales.paid_orders += acc.paid_orders
        for acc in production.values():
            total_production.days += acc.days
            total_production.boards += acc.boards
            total_production.cut_linear_m += acc.cut_linear_m
            total_production.banded_linear_m += acc.banded_linear_m
        total = BranchFigures(
            branch_id=None,
            branch_name="Total",
            sales=_sales_figures(total_sales),
            orders=OrderFigures(
                entered=sum(entered.values()), finished=sum(finished.values())
            ),
            production=_production_figures(total_production),
        )
        return BranchComparison(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            idle_minutes=config.PRODUCTION_IDLE_MINUTES,
            branches=rows,
            total=total,
        )

    # --------------------------------------------------------------- production
    def production_report(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> ProductionReport:
        gap = idle_gap()
        by_branch: dict[int, list[datetime]] = defaultdict(list)
        for event in self.production.cut_events(dr.start, dr.end, branch_id):
            by_branch[event.branch_id].append(event.at)

        day_rows: dict[tuple[int, date], dict] = defaultdict(
            lambda: {
                "workday": None,
                "boards": 0.0,
                "meters": 0.0,
                "banded": 0.0,
                "finished": 0,
            }
        )
        stops = []
        for bid, times in by_branch.items():
            for workday in workdays(times, gap):
                day_rows[(bid, workday.day)]["workday"] = workday
                stops += [(bid, stop) for stop in workday.stops]
        for board in self.production.processed_boards(dr.start, dr.end, branch_id):
            row = day_rows[(board.branch_id, local_date(board.done_at))]
            row["boards"] += board.weight
            row["meters"] += board.cut_linear_m
        for banding in self._closed_activities(
            dr, branch_id, kinds=(ActivityType.banding,)
        ):
            row = day_rows[(banding.branch_id, local_date(banding.finished_at))]
            row["banded"] += banding.banded_m
        finished_by_branch: dict[int, list[int]] = defaultdict(list)
        for bid, order_id, at in self._finished_orders(dr, branch_id):
            day_rows[(bid, local_date(at))]["finished"] += 1
            finished_by_branch[bid].append(order_id)

        names = {
            b.id: b.name
            for b in self._branches(
                {bid for bid, _ in day_rows} | set(finished_by_branch)
            )
        }
        days = []
        for (bid, day), row in sorted(
            day_rows.items(), key=lambda item: (-item[0][1].toordinal(), item[0][0])
        ):
            workday: Optional[Workday] = row["workday"]
            effective = workday.effective_seconds if workday else 0.0
            boards_per_hour, meters_per_hour = _rates(
                row["boards"], row["meters"], effective
            )
            days.append(
                ProductionDay(
                    date=day,
                    branch_id=bid,
                    branch_name=names.get(bid, ""),
                    first_event_at=workday.first_at if workday else None,
                    last_event_at=workday.last_at if workday else None,
                    effective_hours=_hours(effective),
                    paused_hours=_hours(workday.paused_seconds if workday else 0.0),
                    boards=round(row["boards"], 2),
                    cut_linear_m=round(row["meters"], 2),
                    banded_linear_m=round(row["banded"], 2),
                    orders_finished=row["finished"],
                    boards_per_hour=boards_per_hour,
                    meters_per_hour=meters_per_hour,
                )
            )

        stops.sort(key=lambda item: (-item[1].seconds, item[1].start))
        stop_rows = [
            ProductionStop(
                branch_id=bid,
                branch_name=names.get(bid, ""),
                started_at=stop.start,
                ended_at=stop.end,
                minutes=round(stop.seconds / 60.0),
            )
            for bid, stop in stops[:_STOPS_LIMIT]
        ]

        material = []
        for bid in sorted(finished_by_branch):
            efficiency, area = self._efficiency_and_area(finished_by_branch[bid])
            material.append(
                BranchMaterial(
                    branch_id=bid,
                    branch_name=names.get(bid, ""),
                    average_efficiency=efficiency,
                    area_cut_m2=area,
                    waste_estimate_m2=round(area * (1 - efficiency / 100), 4),
                )
            )
        return ProductionReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            idle_minutes=config.PRODUCTION_IDLE_MINUTES,
            days=days,
            stops=stop_rows,
            material=material,
        )

    # ------------------------------------------------------------------ sellers
    def sellers(self, dr: DateRange, branch_id: Optional[int] = None) -> SellerReport:
        """Collected sales per seller, plus each one's orders still to collect."""
        sales: dict[Optional[int], _Sales] = defaultdict(_Sales)
        total_sales = _Sales()
        for _, created_by, cash, transfer, credit, _ in self._paid_orders(
            dr, branch_id
        ):
            sales[created_by].add(cash, transfer, credit)
            total_sales.add(cash, transfer, credit)

        pending_q = self.db.query(
            OrderModel.created_by,
            func.count(OrderModel.id),
            func.coalesce(func.sum(OrderModel.total), 0.0),
        ).filter(OrderModel.status == OrderStatus.confirmed.value)
        if branch_id is not None:
            pending_q = pending_q.filter(OrderModel.branch_id == branch_id)
        pending = {
            uid: (count, amount)
            for uid, count, amount in pending_q.group_by(OrderModel.created_by).all()
        }

        users, branch_names = self._users(uid for uid in set(sales) | set(pending))
        rows = []
        for uid in set(sales) | set(pending):
            user = users.get(uid)
            acc = sales.get(uid) or _Sales()
            count, amount = pending.get(uid, (0, 0.0))
            rows.append(
                SellerRow(
                    user_id=uid,
                    full_name=(user.full_name or user.email)
                    if user
                    else "Sin vendedor",
                    branch_name=branch_names.get(user.branch_id) if user else None,
                    **self._seller_figures(acc, count, amount).model_dump(),
                )
            )
        rows.sort(key=lambda r: (-r.total, -r.pending_amount, r.full_name))
        total_pending = list(pending.values())
        return SellerReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            sellers=rows,
            total=self._seller_figures(
                total_sales,
                sum(c for c, _ in total_pending),
                sum(a for _, a in total_pending),
            ),
        )

    @staticmethod
    def _seller_figures(acc: _Sales, pending_count, pending_amount) -> SellerFigures:
        return SellerFigures(
            paid_orders=acc.paid_orders,
            cash=round(acc.cash, 2),
            credit=round(acc.credit, 2),
            total=round(acc.total, 2),
            average_ticket=round(safe_div(acc.total, acc.paid_orders), 2),
            pending_count=pending_count,
            pending_amount=round(pending_amount or 0.0, 2),
        )

    # ---------------------------------------------------------------- operators
    def operators(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> OperatorReport:
        """The cut per operator: pieces they marked, sheets they closed, hours."""
        gap = idle_gap()
        times: dict[int, list[datetime]] = defaultdict(list)
        pieces: dict[int, int] = defaultdict(int)
        orders: dict[int, set] = defaultdict(set)
        for event in self.production.cut_events(dr.start, dr.end, branch_id):
            if event.user_id is None:
                continue
            times[event.user_id].append(event.at)
            if event.kind == "piece":
                pieces[event.user_id] += 1
                orders[event.user_id].add(event.order_id)
        boards: dict[int, _Production] = defaultdict(_Production)
        for board in self.production.processed_boards(dr.start, dr.end, branch_id):
            if board.cut_by is not None:
                boards[board.cut_by].add_board(board)

        users, branch_names = self._users(set(pieces) | set(boards))
        rows, total_seconds = [], 0.0
        for uid in set(pieces) | set(boards):
            user = users.get(uid)
            if user is None:
                continue
            effective = sum(d.effective_seconds for d in workdays(times[uid], gap))
            total_seconds += effective
            acc = boards[uid]
            boards_per_hour, meters_per_hour = _rates(
                acc.boards, acc.cut_linear_m, effective
            )
            rows.append(
                OperatorRow(
                    user_id=uid,
                    full_name=user.full_name or user.email,
                    branch_name=branch_names.get(user.branch_id),
                    pieces_cut=pieces[uid],
                    boards=round(acc.boards, 2),
                    cut_linear_m=round(acc.cut_linear_m, 2),
                    effective_hours=_hours(effective),
                    boards_per_hour=boards_per_hour,
                    meters_per_hour=meters_per_hour,
                    orders_cut=len(orders[uid]),
                )
            )
        rows.sort(key=lambda r: (-r.boards, -r.pieces_cut, r.full_name))

        total_boards = sum(b.boards for b in boards.values())
        total_meters = sum(b.cut_linear_m for b in boards.values())
        boards_per_hour, meters_per_hour = _rates(
            total_boards, total_meters, total_seconds
        )
        return OperatorReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            idle_minutes=config.PRODUCTION_IDLE_MINUTES,
            operators=rows,
            total=OperatorFigures(
                pieces_cut=sum(pieces.values()),
                boards=round(total_boards, 2),
                cut_linear_m=round(total_meters, 2),
                effective_hours=_hours(total_seconds),
                boards_per_hour=boards_per_hour,
                meters_per_hour=meters_per_hour,
                # Distinct across operators: two of them on one order is one order.
                orders_cut=len(set().union(*orders.values())) if orders else 0,
            ),
        )

    # ------------------------------------------------------------------ banders
    def banders(self, dr: DateRange, branch_id: Optional[int] = None) -> BanderReport:
        """Banding and additional work closed in range, per who closed it.

        Metres are the net tape of each banded order (``_closed_activities``).
        """
        acc: dict[int, dict] = defaultdict(
            lambda: {
                "banded": 0,
                "banding_s": 0.0,
                "banding_m": 0.0,
                "additional": 0,
                "additional_s": 0.0,
            }
        )
        for activity in self._closed_activities(dr, branch_id):
            if activity.finished_by is None:
                continue
            seconds = (
                (activity.finished_at - activity.started_at).total_seconds()
                if activity.started_at
                else 0.0
            )
            a = acc[activity.finished_by]
            if activity.kind == ActivityType.banding.value:
                a["banded"] += 1
                a["banding_s"] += seconds
                a["banding_m"] += activity.banded_m
            else:
                a["additional"] += 1
                a["additional_s"] += seconds

        users, branch_names = self._users(acc.keys())
        rows = []
        for uid, a in acc.items():
            user = users.get(uid)
            if user is None:
                continue
            rows.append(
                BanderRow(
                    user_id=uid,
                    full_name=user.full_name or user.email,
                    branch_name=branch_names.get(user.branch_id),
                    **self._bander_figures(a).model_dump(),
                )
            )
        rows.sort(key=lambda r: (-(r.orders_banded + r.orders_additional), r.full_name))
        total = {
            key: sum(a[key] for a in acc.values())
            for key in (
                "banded",
                "banding_s",
                "banding_m",
                "additional",
                "additional_s",
            )
        }
        return BanderReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            banders=rows,
            total=self._bander_figures(total),
        )

    def _closed_activities(
        self,
        dr: DateRange,
        branch_id: Optional[int] = None,
        kinds: tuple[ActivityType, ...] = (
            ActivityType.banding,
            ActivityType.additional,
        ),
    ) -> list[_ClosedActivity]:
        """Banding and additional work closed in range, dated by the closing.

        A closed banding carries its order's NET tape
        (``total_edge_banding_linear_m`` of the frozen snapshot: the edges of
        every placed piece, special edges included) -- what the bander actually
        applied. The billed metres carry the waste factor on top and would count
        tape nobody laid. Only that key of the snapshot is read.
        """
        query = (
            self.db.query(
                OrderActivityModel.order_id,
                OrderModel.branch_id,
                OrderActivityModel.type,
                OrderActivityModel.started_at,
                OrderActivityModel.finished_at,
                OrderActivityModel.finished_by,
            )
            .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderActivityModel.type.in_([k.value for k in kinds]),
                OrderActivityModel.finished_at >= dr.start,
                OrderActivityModel.finished_at < dr.end,
            )
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        rows = query.all()
        banded = {r.order_id for r in rows if r.type == ActivityType.banding.value}
        meters: dict[int, float] = {}
        if banded:
            for order_id, value in (
                self.db.query(
                    OrderModel.id,
                    OrderModel.optimization_snapshot["total_edge_banding_linear_m"],
                )
                .filter(OrderModel.id.in_(list(banded)))
                .all()
            ):
                try:
                    meters[order_id] = float(value or 0.0)
                except (TypeError, ValueError):
                    meters[order_id] = 0.0
        return [
            _ClosedActivity(
                order_id=r.order_id,
                branch_id=r.branch_id,
                kind=r.type,
                started_at=r.started_at,
                finished_at=r.finished_at,
                finished_by=r.finished_by,
                banded_m=meters.get(r.order_id, 0.0)
                if r.type == ActivityType.banding.value
                else 0.0,
            )
            for r in rows
        ]

    @staticmethod
    def _bander_figures(a: dict) -> BanderFigures:
        return BanderFigures(
            orders_banded=a["banded"],
            banding_hours=_hours(a["banding_s"]),
            banded_linear_m=round(a["banding_m"], 2),
            # Over the activity's start-to-close hours, stops included: the
            # banding marks no pieces, so this rate reads lower than the cut's.
            banding_meters_per_hour=round(
                safe_div(a["banding_m"], a["banding_s"] / 3600.0), 2
            ),
            average_banding_hours=_hours(safe_div(a["banding_s"], a["banded"])),
            orders_additional=a["additional"],
            additional_hours=_hours(a["additional_s"]),
            average_additional_hours=_hours(
                safe_div(a["additional_s"], a["additional"])
            ),
        )

    # ------------------------------------------------------------------ helpers
    def _paid_orders(self, dr: DateRange, branch_id: Optional[int] = None) -> list:
        """``(branch_id, created_by, cash, transfer, credit, queued_at)`` of the sales.

        A sale is placed by its payment: the amounts are frozen on entering the
        queue, which is gated on registering them. Cancelled orders are out --
        their amounts survive as a record, but nobody keeps that money.
        """
        query = self.db.query(
            OrderModel.branch_id,
            OrderModel.created_by,
            OrderModel.payment_cash_amount,
            OrderModel.payment_transfer_amount,
            OrderModel.payment_credit_amount,
            OrderModel.queued_at,
        ).filter(
            OrderModel.queued_at >= dr.start,
            OrderModel.queued_at < dr.end,
            OrderModel.status != OrderStatus.cancelled.value,
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        return query.all()

    def _finished_orders(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> list[tuple[int, int, datetime]]:
        """``(branch_id, order_id, finished_at)`` of the orders closed in range.

        Read off the history, so a closing is dated when it happened. The
        machine has no way back from ``finished``; the earliest row still wins
        should an order carry two.
        """
        query = (
            self.db.query(
                OrderModel.branch_id,
                OrderStatusHistoryModel.order_id,
                func.min(OrderStatusHistoryModel.created_at),
            )
            .join(OrderModel, OrderStatusHistoryModel.order_id == OrderModel.id)
            .filter(
                OrderStatusHistoryModel.to_status == OrderStatus.finished.value,
                OrderStatusHistoryModel.created_at >= dr.start,
                OrderStatusHistoryModel.created_at < dr.end,
            )
            .group_by(OrderModel.branch_id, OrderStatusHistoryModel.order_id)
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        return query.all()

    def _production_by_branch(self, dr: DateRange) -> dict[int, _Production]:
        gap = idle_gap()
        acc: dict[int, _Production] = defaultdict(_Production)
        times: dict[int, list[datetime]] = defaultdict(list)
        for event in self.production.cut_events(dr.start, dr.end):
            times[event.branch_id].append(event.at)
        for bid, branch_times in times.items():
            acc[bid].days = workdays(branch_times, gap)
        for board in self.production.processed_boards(dr.start, dr.end):
            acc[board.branch_id].add_board(board)
        for banding in self._closed_activities(dr, kinds=(ActivityType.banding,)):
            acc[banding.branch_id].banded_linear_m += banding.banded_m
        return acc

    def _efficiency_and_area(self, order_ids: list[int]) -> tuple[float, float]:
        """Area-weighted efficiency (0..100) and area (m²) of the board lines.

        Same weighting as ``AnalyticsService._efficiency_and_area``; edge-banding
        lines carry no area and stay out.
        """
        rows = (
            self.db.query(OrderLineModel.avg_efficiency, OrderLineModel.total_area_m2)
            .filter(OrderLineModel.order_id.in_(order_ids))
            .filter(OrderLineModel.total_area_m2.isnot(None))
            .filter(OrderLineModel.avg_efficiency.isnot(None))
            .all()
        )
        total_area = sum(area for _, area in rows)
        if not total_area:
            return 0.0, 0.0
        weighted = sum(eff * area for eff, area in rows) / total_area
        return round(weighted, 2), round(total_area, 4)

    def _branches(self, with_data: set[int]) -> list[BranchModel]:
        """Active branches plus any inactive one that has data in range.

        Densified so the comparison's columns never move; an inactive branch
        still shows when it carries figures, or the total would not add up.
        """
        query = self.db.query(BranchModel)
        branches = query.order_by(BranchModel.id).all()
        return [b for b in branches if b.is_active or b.id in with_data]

    def _users(self, user_ids: Iterable[Optional[int]]) -> tuple[dict, dict]:
        """Users by id and the ``branch_id -> name`` map."""
        ids = [uid for uid in user_ids if uid is not None]
        users = (
            {
                u.id: u
                for u in self.db.query(UserModel).filter(UserModel.id.in_(ids)).all()
            }
            if ids
            else {}
        )
        names = {b.id: b.name for b in self.db.query(BranchModel).all()}
        return users, names


def performance_service(db: Session = Depends(get_db)) -> PerformanceService:
    """``PerformanceService`` provider for injection into routes."""
    return PerformanceService(db)
