"""Branch performance: the comparison, the production tab and productivity by role.

Every figure is placed in the period by WHEN IT HAPPENED, not by when its order
was created: a sale by the payment (``queued_at``), a sheet by its last piece
cut, a closed order by its ``finished`` history row, an activity by its close.
The cutting rules (the cut's time, board weight, saw metres) are the shop
floor's own, in ``orders/production.py``, read through ``ProductionService``.
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
    BanderOrder,
    BanderOrdersReport,
    BanderReport,
    BanderRow,
    BranchComparison,
    BranchFigures,
    BranchMaterial,
    OperatorBoard,
    OperatorBoardsReport,
    OperatorFigures,
    OperatorOrderTime,
    OperatorReport,
    OperatorRow,
    OrderFigures,
    PauseTotal,
    ProductionDay,
    ProductionFigures,
    ProductionPause,
    ProductionReport,
    RangeInfo,
    SalesFigures,
    SellerCollection,
    SellerFigures,
    SellerOrder,
    SellerOrdersReport,
    SellerOutstandingOrder,
    SellerPendingOrder,
    SellerReport,
    SellerRow,
)
from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    ActivityType,
    OrderActivityModel,
    OrderLineModel,
    OrderModel,
    OrderPaymentModel,
    OrderStatus,
    OrderStatusHistoryModel,
)
from src.modules.orders.payments import collected_expr, credit_balance_expr, has_balance
from src.modules.orders.pieces_export import customer_name
from src.modules.orders.production import (
    SheetCredit,
    WorkSegment,
    activity_seconds,
    paused_by_day,
    sheet_credit,
    sheet_kind,
    work_by_day,
)
from src.modules.orders.production_service import (
    OrderCut,
    PauseRecord,
    ProcessedBoard,
    ProductionService,
)
from src.modules.users.model import UserModel
from src.shared.business_time import local_date, minute_of_day
from src.shared.config import config
from src.shared.database import get_db
from src.shared.exceptions import EntityNotFoundError
from src.shared.work_schedule import WorkSchedule, seconds

# The row of the work whose user is gone: ``cut_by``/``finished_by`` went NULL
# when the user was deleted. Listed, so the role's total still matches the
# branch comparison, which counts that work too.
_NO_USER = "Sin usuario"


@dataclass
class _Sales:
    cash: float = 0.0
    credit: float = 0.0
    paid_orders: int = 0
    # Credit paid back in the range, and what is owed right now (a snapshot).
    collected: float = 0.0
    outstanding: float = 0.0
    outstanding_orders: int = 0

    def add(self, cash, transfer, credit) -> None:
        # Two methods as the office reads them: received (cash + transfer) and owed.
        self.cash += (cash or 0.0) + (transfer or 0.0)
        self.credit += credit or 0.0
        self.paid_orders += 1

    @property
    def total(self) -> float:
        return self.cash + self.credit


@dataclass
class _WorkDay:
    """One business day of one branch's saw.

    ``first_at``/``last_at``: the day's first and last cutting event (null on a
    day that only holds the tail of a stretch). The seconds are the cut's time
    of the day and the working time it spent in a registered pause.
    """

    day: date
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    loading_seconds: float = 0.0
    sawing_seconds: float = 0.0
    paused_seconds: float = 0.0

    @property
    def work_seconds(self) -> float:
        return self.loading_seconds + self.sawing_seconds

    def see(self, at: datetime) -> None:
        self.first_at = at if self.first_at is None else min(self.first_at, at)
        self.last_at = at if self.last_at is None else max(self.last_at, at)


@dataclass
class _Production:
    days: list[_WorkDay] = field(default_factory=list)
    boards: float = 0.0
    cut_linear_m: float = 0.0
    banded_linear_m: float = 0.0
    orders_banded: int = 0
    orders_additional: int = 0

    def add_board(self, board: ProcessedBoard) -> None:
        self.boards += board.weight
        self.cut_linear_m += board.cut_linear_m

    def add_closed(self, activity: "_ClosedActivity") -> None:
        if activity.kind == ActivityType.banding.value:
            self.orders_banded += 1
            self.banded_linear_m += activity.banded_m
        else:
            self.orders_additional += 1

    def merge(self, other: "_Production") -> None:
        self.days += other.days
        self.boards += other.boards
        self.cut_linear_m += other.cut_linear_m
        self.banded_linear_m += other.banded_linear_m
        self.orders_banded += other.orders_banded
        self.orders_additional += other.orders_additional


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


def _rates(boards: float, meters: float, work_seconds: float) -> tuple:
    """Boards and metres per hour of the cut's time (loading + sawing)."""
    hours = work_seconds / 3600.0
    return round(safe_div(boards, hours), 2), round(safe_div(meters, hours), 2)


def _paused_seconds(
    pauses: Iterable[PauseRecord], schedule: WorkSchedule, now: datetime
) -> float:
    """Working time a set of pauses took, each on its own (no union)."""
    return sum(seconds(schedule.overlap(*pause.interval(now))) for pause in pauses)


def _work_seconds(days: dict) -> tuple[float, float]:
    """``(loading, sawing)`` seconds over the days of a ``work_by_day``."""
    return (
        sum(day.loading_seconds for day in days.values()),
        sum(day.sawing_seconds for day in days.values()),
    )


def _production_figures(acc: _Production) -> ProductionFigures:
    """Projects a branch's (or the total's) production accumulator."""
    loading = sum(d.loading_seconds for d in acc.days)
    sawing = sum(d.sawing_seconds for d in acc.days)
    work = loading + sawing
    paused = sum(d.paused_seconds for d in acc.days)
    # A day with an event and no work (somebody took an order and nothing
    # else) is not a workday: it would drag the average start to whatever time
    # that happened.
    worked = [d for d in acc.days if d.work_seconds > 0 and d.first_at is not None]
    boards_per_hour, meters_per_hour = _rates(acc.boards, acc.cut_linear_m, work)
    return ProductionFigures(
        boards=round(acc.boards, 2),
        cut_linear_m=round(acc.cut_linear_m, 2),
        banded_linear_m=round(acc.banded_linear_m, 2),
        orders_banded=acc.orders_banded,
        orders_additional=acc.orders_additional,
        work_hours=_hours(work),
        loading_hours=_hours(loading),
        sawing_hours=_hours(sawing),
        effective_hours=_hours(work),
        paused_hours=_hours(paused),
        boards_per_hour=boards_per_hour,
        meters_per_hour=meters_per_hour,
        # Calendar days: on the total, two branches cutting the same day is one day.
        days_worked=len({d.day for d in acc.days if d.work_seconds > 0}),
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
    # Its worked time (``activity_seconds``: pauses and time off are out), or
    # ``None`` when it was registered after the work.
    clocked_seconds: Optional[float] = None
    # Working time its registered pauses took.
    paused_seconds: float = 0.0


@dataclass
class _BanderAcc:
    """Banding and additional work closed, with the time of the CLOCKED ones only.

    A start and a close registered together carry no duration
    (``clocked_seconds``): the work counts, its seconds do not, and neither do
    its metres in the metres per hour -- they would read as tape laid in no time.
    """

    banded: int = 0
    banded_unclocked: int = 0
    banded_m: float = 0.0
    banding_s: float = 0.0
    banding_clocked_m: float = 0.0
    additional: int = 0
    additional_unclocked: int = 0
    additional_s: float = 0.0
    paused_s: float = 0.0

    def add(self, activity: "_ClosedActivity") -> None:
        seconds = activity.clocked_seconds
        self.paused_s += activity.paused_seconds
        if activity.kind == ActivityType.banding.value:
            self.banded += 1
            self.banded_m += activity.banded_m
            if seconds is None:
                self.banded_unclocked += 1
            else:
                self.banding_s += seconds
                self.banding_clocked_m += activity.banded_m
        else:
            self.additional += 1
            if seconds is None:
                self.additional_unclocked += 1
            else:
                self.additional_s += seconds

    def figures(self) -> BanderFigures:
        banded_clocked = self.banded - self.banded_unclocked
        additional_clocked = self.additional - self.additional_unclocked
        return BanderFigures(
            orders_banded=self.banded,
            orders_banded_unclocked=self.banded_unclocked,
            banding_hours=_hours(self.banding_s),
            banded_linear_m=round(self.banded_m, 2),
            # Over the activity's start-to-close hours, less pauses and time
            # off: the banding marks no pieces, so that is all there is.
            banding_meters_per_hour=round(
                safe_div(self.banding_clocked_m, self.banding_s / 3600.0), 2
            ),
            average_banding_hours=_hours(safe_div(self.banding_s, banded_clocked)),
            orders_additional=self.additional,
            orders_additional_unclocked=self.additional_unclocked,
            additional_hours=_hours(self.additional_s),
            average_additional_hours=_hours(
                safe_div(self.additional_s, additional_clocked)
            ),
            paused_hours=_hours(self.paused_s),
        )


def _sales_figures(acc: _Sales) -> SalesFigures:
    return SalesFigures(
        cash=round(acc.cash, 2),
        credit=round(acc.credit, 2),
        total=round(acc.total, 2),
        paid_orders=acc.paid_orders,
        credit_collected=round(acc.collected, 2),
        credit_outstanding=round(acc.outstanding, 2),
        credit_outstanding_orders=acc.outstanding_orders,
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
        for branch_id, _, amount in self._collected(dr):
            sales[branch_id].collected += amount
        for row in self._outstanding():
            sales[row.branch_id].outstanding += row.balance
            sales[row.branch_id].outstanding_orders += 1

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
            total_sales.collected += acc.collected
            total_sales.outstanding += acc.outstanding
            total_sales.outstanding_orders += acc.outstanding_orders
        for acc in production.values():
            total_production.merge(acc)
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
        work_days, _ = self._work_days(dr, branch_id)

        day_rows: dict[tuple[int, date], dict] = defaultdict(
            lambda: {
                "boards": 0.0,
                "meters": 0.0,
                "banded": 0.0,
                "orders_banded": 0,
                "orders_additional": 0,
                "finished": 0,
            }
        )
        for bid, days in work_days.items():
            for day in days:
                day_rows[(bid, day)]  # a day of work lists even with no board closed
        for board in self.production.processed_boards(dr.start, dr.end, branch_id):
            row = day_rows[(board.branch_id, local_date(board.done_at))]
            row["boards"] += board.weight
            row["meters"] += board.cut_linear_m
        for activity in self._closed_activities(dr, branch_id):
            row = day_rows[(activity.branch_id, local_date(activity.finished_at))]
            if activity.kind == ActivityType.banding.value:
                row["banded"] += activity.banded_m
                row["orders_banded"] += 1
            else:
                row["orders_additional"] += 1
        finished_by_branch: dict[int, list[int]] = defaultdict(list)
        for bid, order_id, at in self._finished_orders(dr, branch_id):
            day_rows[(bid, local_date(at))]["finished"] += 1
            finished_by_branch[bid].append(order_id)

        pauses = self.production.pauses(dr.start, dr.end, branch_id)
        names = {
            b.id: b.name
            for b in self._branches(
                {bid for bid, _ in day_rows}
                | set(finished_by_branch)
                | {pause.branch_id for pause in pauses}
            )
        }
        days = []
        for (bid, day), row in sorted(
            day_rows.items(), key=lambda item: (-item[0][1].toordinal(), item[0][0])
        ):
            workday = work_days.get(bid, {}).get(day) or _WorkDay(day=day)
            boards_per_hour, meters_per_hour = _rates(
                row["boards"], row["meters"], workday.work_seconds
            )
            days.append(
                ProductionDay(
                    date=day,
                    branch_id=bid,
                    branch_name=names.get(bid, ""),
                    first_event_at=workday.first_at,
                    last_event_at=workday.last_at,
                    work_hours=_hours(workday.work_seconds),
                    loading_hours=_hours(workday.loading_seconds),
                    sawing_hours=_hours(workday.sawing_seconds),
                    effective_hours=_hours(workday.work_seconds),
                    paused_hours=_hours(workday.paused_seconds),
                    boards=round(row["boards"], 2),
                    cut_linear_m=round(row["meters"], 2),
                    banded_linear_m=round(row["banded"], 2),
                    orders_banded=row["orders_banded"],
                    orders_additional=row["orders_additional"],
                    orders_finished=row["finished"],
                    boards_per_hour=boards_per_hour,
                    meters_per_hour=meters_per_hour,
                )
            )

        pause_rows, pause_totals = self._pause_rows(pauses, names)

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
            stops=[],
            pauses=pause_rows,
            pause_totals=pause_totals,
            material=material,
        )

    def _pause_rows(
        self, pauses: list[PauseRecord], branch_names: dict[int, str]
    ) -> tuple[list[ProductionPause], list[PauseTotal]]:
        """The pauses as the production tab lists them, newest first, and by reason.

        ``hours`` is each pause's own working time, start to end (or to now) --
        not clipped to the range, so the same pause reads the same in two
        reports that both overlap it.
        """
        now = datetime.utcnow()
        schedule = self.production.schedule
        refs = self._order_refs({pause.order_id for pause in pauses})
        rows, totals = [], defaultdict(lambda: [0, 0.0])
        for pause in sorted(pauses, key=lambda p: (p.started_at, p.id), reverse=True):
            code, client, _ = refs[pause.order_id]
            took = _paused_seconds([pause], schedule, now)
            totals[pause.reason][0] += 1
            totals[pause.reason][1] += took
            rows.append(
                ProductionPause(
                    id=pause.id,
                    branch_id=pause.branch_id,
                    branch_name=branch_names.get(pause.branch_id, ""),
                    order_id=pause.order_id,
                    order_code=code,
                    client_name=client,
                    activity=pause.activity,
                    reason=pause.reason,
                    note=pause.note,
                    started_at=pause.started_at,
                    started_by=pause.started_by_label,
                    ended_at=pause.ended_at,
                    ended_by=pause.ended_by_label,
                    hours=_hours(took),
                )
            )
        return rows, [
            PauseTotal(reason=reason, count=count, hours=_hours(took))
            for reason, (count, took) in sorted(
                totals.items(), key=lambda item: (-item[1][1], item[0])
            )
        ]

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
        for _, created_by, amount in self._collected(dr, branch_id):
            sales[created_by].collected += amount
            total_sales.collected += amount
        for row in self._outstanding(branch_id):
            sales[row.created_by].outstanding += row.balance
            total_sales.outstanding += row.balance

        pending_q = self.db.query(
            OrderModel.created_by,
            func.count(OrderModel.id),
            func.coalesce(func.sum(OrderModel.total), 0.0),
        ).filter(*self._pending_filters(branch_id))
        pending = {
            uid: (count, amount)
            for uid, count, amount in pending_q.group_by(OrderModel.created_by).all()
        }

        users, branch_names = self._users(uid for uid in set(sales) | set(pending))
        rows = []
        # ``sales`` also holds whoever only collected credit, or is only owed.
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

    def seller_orders(
        self, user_id: int, dr: DateRange, branch_id: Optional[int] = None
    ) -> SellerOrdersReport:
        """The orders behind one seller's row: what they collected, what is owed.

        Same filters as ``sellers`` (``_sale_filters``, ``_pending_filters``,
        ``_collection_filters``, ``_outstanding``), so the figures add up to
        the row by construction. Each sale carries its invoice number, to be
        checked against the accounting system.
        """
        user = self._user_or_404(user_id)
        paid = (
            self.db.query(
                OrderModel.id,
                OrderModel.created_at,
                OrderModel.queued_at,
                OrderModel.payment_cash_amount,
                OrderModel.payment_transfer_amount,
                OrderModel.payment_credit_amount,
                OrderModel.external_invoice_id,
                collected_expr().label("collected"),
            )
            .filter(*self._sale_filters(dr, branch_id))
            .filter(OrderModel.created_by == user_id)
            .order_by(OrderModel.queued_at.desc(), OrderModel.id.desc())
            .all()
        )
        pending = (
            self.db.query(OrderModel.id, OrderModel.confirmed_at, OrderModel.total)
            .filter(*self._pending_filters(branch_id))
            .filter(OrderModel.created_by == user_id)
            .order_by(OrderModel.confirmed_at, OrderModel.id)
            .all()
        )
        collections = (
            self.db.query(
                OrderPaymentModel,
                OrderModel.queued_at,
            )
            .join(OrderModel, OrderPaymentModel.order_id == OrderModel.id)
            .filter(*self._collection_filters(dr, branch_id))
            .filter(OrderModel.created_by == user_id)
            .order_by(OrderPaymentModel.paid_at.desc(), OrderPaymentModel.id.desc())
            .all()
        )
        outstanding = [
            row for row in self._outstanding(branch_id) if row.created_by == user_id
        ]
        refs = self._order_refs(
            [r.id for r in paid]
            + [r.id for r in pending]
            + [payment.order_id for payment, _ in collections]
            + [row.id for row in outstanding]
        )
        _, branch_names = self._users([])

        def ref(order_id):
            code, client, bid = refs[order_id]
            return {
                "order_id": order_id,
                "order_code": code,
                "client_name": client,
                "branch_name": branch_names.get(bid, ""),
            }

        acc = _Sales()
        paid_rows = []
        for r in paid:
            cash, transfer, credit = (
                r.payment_cash_amount or 0.0,
                r.payment_transfer_amount or 0.0,
                r.payment_credit_amount or 0.0,
            )
            acc.add(cash, transfer, credit)
            collected = r.collected or 0.0
            paid_rows.append(
                SellerOrder(
                    **ref(r.id),
                    created_at=r.created_at,
                    paid_at=r.queued_at,
                    day=local_date(r.queued_at),
                    cash=round(cash, 2),
                    transfer=round(transfer, 2),
                    credit=round(credit, 2),
                    total=round(cash + transfer + credit, 2),
                    invoice=r.external_invoice_id,
                    credit_collected=round(collected, 2),
                    credit_balance=round(max(credit - collected, 0.0), 2),
                )
            )
        acc.collected = sum(payment.amount for payment, _ in collections)
        acc.outstanding = sum(row.balance for row in outstanding)
        return SellerOrdersReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            user_id=user.id,
            full_name=user.full_name or user.email,
            figures=self._seller_figures(
                acc, len(pending), sum(r.total or 0.0 for r in pending)
            ),
            paid=paid_rows,
            pending=[
                SellerPendingOrder(
                    **ref(r.id),
                    confirmed_at=r.confirmed_at,
                    total=round(r.total or 0.0, 2),
                )
                for r in pending
            ],
            collections=[
                SellerCollection(
                    payment_id=payment.id,
                    **ref(payment.order_id),
                    sold_at=sold_at,
                    paid_at=payment.paid_at,
                    day=local_date(payment.paid_at),
                    method=payment.method,
                    amount=round(payment.amount, 2),
                    received_by=payment.received_by_label,
                    note=payment.note,
                )
                for payment, sold_at in collections
            ],
            outstanding=[
                SellerOutstandingOrder(
                    **ref(row.id),
                    sold_at=row.queued_at,
                    credit=round(row.credit, 2),
                    credit_collected=round(row.collected, 2),
                    credit_balance=round(row.balance, 2),
                )
                for row in outstanding
            ],
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
            credit_collected=round(acc.collected, 2),
            credit_outstanding=round(acc.outstanding, 2),
        )

    # ---------------------------------------------------------------- operators
    def operators(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> OperatorReport:
        """The cut per operator: pieces they marked, sheets they closed, hours.

        The hours are the cut's time of the stretches THEY closed
        (``cut_segments``), split into loading and sawing like the branch's.

        The work of a user deleted since (``cut_by`` NULL) stays, in one
        ``Sin usuario`` row: the total must match the branch comparison.
        """
        pieces: dict[Optional[int], int] = defaultdict(int)
        orders: dict[Optional[int], set] = defaultdict(set)
        for event in self.production.cut_events(dr.start, dr.end, branch_id):
            if event.kind == "piece":
                pieces[event.user_id] += 1
                orders[event.user_id].add(event.order_id)
        boards: dict[Optional[int], _Production] = defaultdict(_Production)
        for board in self.production.processed_boards(dr.start, dr.end, branch_id):
            boards[board.cut_by].add_board(board)

        cuts, _ = self.production.order_cuts(dr.start, dr.end, branch_id)
        segments: dict[Optional[int], list[WorkSegment]] = defaultdict(list)
        for cut in cuts:
            for segment in cut.segments:
                segments[segment.user_id].append(segment)
        work = {
            uid: _work_seconds(work_by_day(segs, dr.start, dr.end))
            for uid, segs in segments.items()
        }
        now = datetime.utcnow()
        paused: dict[Optional[int], list[PauseRecord]] = defaultdict(list)
        for pause in self.production.pauses(
            dr.start, dr.end, branch_id, ActivityType.cutting
        ):
            paused[pause.started_by].append(pause)

        worked = {uid for uid, (loading, sawing) in work.items() if loading + sawing}
        user_ids = set(pieces) | set(boards) | worked
        users, branch_names = self._users(user_ids)
        rows = []
        total_loading = total_sawing = total_paused = 0.0
        for uid in user_ids:
            user = users.get(uid)
            if uid is not None and user is None:
                continue
            loading, sawing = work.get(uid, (0.0, 0.0))
            paused_seconds = _paused_seconds(paused[uid], self.production.schedule, now)
            total_loading += loading
            total_sawing += sawing
            total_paused += paused_seconds
            acc = boards[uid]
            boards_per_hour, meters_per_hour = _rates(
                acc.boards, acc.cut_linear_m, loading + sawing
            )
            rows.append(
                OperatorRow(
                    user_id=uid,
                    full_name=(user.full_name or user.email) if user else _NO_USER,
                    branch_name=branch_names.get(user.branch_id) if user else None,
                    pieces_cut=pieces[uid],
                    boards=round(acc.boards, 2),
                    cut_linear_m=round(acc.cut_linear_m, 2),
                    work_hours=_hours(loading + sawing),
                    loading_hours=_hours(loading),
                    sawing_hours=_hours(sawing),
                    paused_hours=_hours(paused_seconds),
                    pauses=len(paused[uid]),
                    effective_hours=_hours(loading + sawing),
                    boards_per_hour=boards_per_hour,
                    meters_per_hour=meters_per_hour,
                    orders_cut=len(orders[uid]),
                )
            )
        rows.sort(
            key=lambda r: (r.user_id is None, -r.boards, -r.pieces_cut, r.full_name)
        )

        total_boards = sum(b.boards for b in boards.values())
        total_meters = sum(b.cut_linear_m for b in boards.values())
        total_work = total_loading + total_sawing
        boards_per_hour, meters_per_hour = _rates(
            total_boards, total_meters, total_work
        )
        return OperatorReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            idle_minutes=config.PRODUCTION_IDLE_MINUTES,
            operators=rows,
            total=OperatorFigures(
                pieces_cut=sum(pieces.values()),
                boards=round(total_boards, 2),
                cut_linear_m=round(total_meters, 2),
                work_hours=_hours(total_work),
                loading_hours=_hours(total_loading),
                sawing_hours=_hours(total_sawing),
                paused_hours=_hours(total_paused),
                pauses=sum(len(p) for p in paused.values()),
                effective_hours=_hours(total_work),
                boards_per_hour=boards_per_hour,
                meters_per_hour=meters_per_hour,
                # Distinct across operators: two of them on one order is one order.
                orders_cut=len(set().union(*orders.values())) if orders else 0,
            ),
        )

    def operator_boards(
        self, user_id: int, dr: DateRange, branch_id: Optional[int] = None
    ) -> OperatorBoardsReport:
        """Every sheet the operator touched in range, and why it counts or not.

        The validation of their row: the credited sheets add up to its
        ``boards`` and the in-range marks to its ``piecesCut``, by construction
        (``ProductionService.operator_sheets``).
        """
        user = self._user_or_404(user_id)
        _, branch_names = self._users([])
        rows = []
        for sheet in self.production.operator_sheets(
            user_id, dr.start, dr.end, branch_id
        ):
            status = sheet_credit(
                user_id, sheet.closed_by, sheet.done_at, dr.start, dr.end
            )
            rows.append(
                OperatorBoard(
                    board_id=sheet.board_id,
                    order_id=sheet.order_id,
                    order_code=sheet.order_code,
                    client_name=sheet.client_name,
                    branch_name=branch_names.get(sheet.branch_id, ""),
                    sheet_number=sheet.sheet_number,
                    material_name=sheet.material_name,
                    width=sheet.width,
                    height=sheet.height,
                    kind=sheet_kind(sheet.half_board, sheet.source),
                    weight=sheet.weight,
                    day=local_date(sheet.done_at or sheet.my_last_cut_at),
                    pieces_total=sheet.pieces_total,
                    pieces_mine=sheet.pieces_mine,
                    pieces_mine_in_range=sheet.pieces_mine_in_range,
                    pieces_by_others=sheet.pieces_total
                    - sheet.pieces_mine
                    - sheet.pieces_pending,
                    pieces_pending=sheet.pieces_pending,
                    other_cutters=list(sheet.other_cutters),
                    my_last_cut_at=sheet.my_last_cut_at,
                    done_at=sheet.done_at,
                    closed_by=sheet.closed_by_label,
                    status=status,
                )
            )
        # Newest first, like the production tab.
        rows.sort(
            key=lambda r: (r.day, r.done_at or r.my_last_cut_at, r.board_id),
            reverse=True,
        )
        credited = [r for r in rows if r.status is SheetCredit.credited]
        return OperatorBoardsReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            user_id=user.id,
            full_name=user.full_name or user.email,
            boards=round(sum(r.weight for r in credited), 2),
            credited_count=len(credited),
            pieces_cut=sum(r.pieces_mine_in_range for r in rows),
            sheets=rows,
            orders=self._operator_order_times(user_id, dr, branch_id, branch_names),
        )

    def _operator_order_times(
        self,
        user_id: int,
        dr: DateRange,
        branch_id: Optional[int],
        branch_names: dict[int, str],
    ) -> list[OperatorOrderTime]:
        """Order by order, the cut's time behind one operator's hours.

        The same segments ``operators`` adds up (the stretches this user
        closed, clipped to the range), kept apart per order.
        """
        now = datetime.utcnow()
        cuts, _ = self.production.order_cuts(dr.start, dr.end, branch_id)
        timed: list[tuple[OrderCut, float, float]] = []
        for cut in cuts:
            mine = [seg for seg in cut.segments if seg.user_id == user_id]
            loading, sawing = _work_seconds(work_by_day(mine, dr.start, dr.end))
            if loading + sawing:
                timed.append((cut, loading, sawing))
        refs = self._order_refs({cut.order_id for cut, _, _ in timed})
        rows = []
        for cut, loading, sawing in sorted(
            timed,
            key=lambda item: (item[0].last_mark_at, item[0].order_id),
            reverse=True,
        ):
            code, client, bid = refs[cut.order_id]
            rows.append(
                OperatorOrderTime(
                    order_id=cut.order_id,
                    order_code=code,
                    client_name=client,
                    branch_name=branch_names.get(bid, ""),
                    taken_at=cut.taken_at,
                    first_piece_at=cut.first_mark_at,
                    last_piece_at=cut.last_mark_at,
                    loading_hours=_hours(loading),
                    sawing_hours=_hours(sawing),
                    work_hours=_hours(loading + sawing),
                    paused_hours=_hours(
                        _paused_seconds(cut.pauses, self.production.schedule, now)
                    ),
                    pause_reasons=sorted({pause.reason for pause in cut.pauses}),
                )
            )
        return rows

    # ------------------------------------------------------------------ banders
    def banders(self, dr: DateRange, branch_id: Optional[int] = None) -> BanderReport:
        """Banding and additional work closed in range, per who closed it.

        Metres are the net tape of each banded order (``_closed_activities``).
        Time is only the clocked activities' (``_BanderAcc``).
        """
        acc: dict[Optional[int], _BanderAcc] = defaultdict(_BanderAcc)
        total = _BanderAcc()
        for activity in self._closed_activities(dr, branch_id):
            acc[activity.finished_by].add(activity)
            total.add(activity)

        users, branch_names = self._users(acc.keys())
        rows = []
        for uid, a in acc.items():
            user = users.get(uid)
            if uid is not None and user is None:
                continue
            rows.append(
                BanderRow(
                    user_id=uid,
                    full_name=(user.full_name or user.email) if user else _NO_USER,
                    branch_name=branch_names.get(user.branch_id) if user else None,
                    **a.figures().model_dump(),
                )
            )
        rows.sort(
            key=lambda r: (
                r.user_id is None,
                -(r.orders_banded + r.orders_additional),
                r.full_name,
            )
        )
        return BanderReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            banders=rows,
            total=total.figures(),
        )

    def bander_orders(
        self, user_id: int, dr: DateRange, branch_id: Optional[int] = None
    ) -> BanderOrdersReport:
        """The work behind one bander's row: each banding and additional work
        they closed in range, with its time -- or why it has none.

        The same closed activities as ``banders``, so the figures add up to the
        row by construction.
        """
        user = self._user_or_404(user_id)
        closed = [
            a
            for a in self._closed_activities(dr, branch_id)
            if a.finished_by == user_id
        ]
        acc = _BanderAcc()
        for activity in closed:
            acc.add(activity)
        refs = self._order_refs({a.order_id for a in closed})
        _, branch_names = self._users([])
        rows = []
        for a in sorted(
            closed, key=lambda a: (a.finished_at, a.order_id), reverse=True
        ):
            code, client, bid = refs[a.order_id]
            clocked = a.clocked_seconds
            rows.append(
                BanderOrder(
                    order_id=a.order_id,
                    order_code=code,
                    client_name=client,
                    branch_name=branch_names.get(bid, ""),
                    kind=a.kind,
                    started_at=a.started_at,
                    finished_at=a.finished_at,
                    day=local_date(a.finished_at),
                    hours=None if clocked is None else _hours(clocked),
                    paused_hours=_hours(a.paused_seconds),
                    banded_linear_m=round(a.banded_m, 2),
                )
            )
        return BanderOrdersReport(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            user_id=user.id,
            full_name=user.full_name or user.email,
            figures=acc.figures(),
            orders=rows,
        )

    def _closed_activities(
        self, dr: DateRange, branch_id: Optional[int] = None
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
                OrderActivityModel.id,
                OrderActivityModel.order_id,
                OrderModel.branch_id,
                OrderActivityModel.type,
                OrderActivityModel.started_at,
                OrderActivityModel.finished_at,
                OrderActivityModel.finished_by,
            )
            .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
            .filter(
                OrderActivityModel.type.in_(
                    [ActivityType.banding.value, ActivityType.additional.value]
                ),
                OrderActivityModel.finished_at >= dr.start,
                OrderActivityModel.finished_at < dr.end,
            )
        )
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        rows = query.all()
        meters = self.production.banded_meters_of(
            {r.order_id for r in rows if r.type == ActivityType.banding.value}
        )

        # Their time: start to close, less the pauses and the time off it ran
        # across. A day counts as worked when the branch's banders registered
        # something on it -- theirs, not the saw's, which can cut on a day
        # nobody bands.
        pauses = self.production.pauses_of(r.id for r in rows)
        schedule = self.production.schedule
        now = datetime.utcnow()
        starts = [r.started_at for r in rows if r.started_at is not None]
        open_days = (
            self._bander_open_days(min(starts), dr.end, branch_id) if starts else {}
        )
        closed = []
        for r in rows:
            own = pauses.get(r.id, [])
            closed.append(
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
                    clocked_seconds=activity_seconds(
                        r.started_at,
                        r.finished_at,
                        [(pause.started_at, pause.ended_at) for pause in own],
                        schedule,
                        open_days.get(r.branch_id),
                    ),
                    paused_seconds=_paused_seconds(own, schedule, now),
                )
            )
        return closed

    def _bander_open_days(
        self, start: datetime, end: datetime, branch_id: Optional[int] = None
    ) -> dict[int, set[date]]:
        """``branch_id -> business days`` a banding or additional work was
        started or closed on, in ``[start, end)``."""
        days: dict[int, set[date]] = defaultdict(set)
        for column in (OrderActivityModel.started_at, OrderActivityModel.finished_at):
            query = (
                self.db.query(OrderModel.branch_id, column)
                .join(OrderModel, OrderActivityModel.order_id == OrderModel.id)
                .filter(
                    OrderActivityModel.type.in_(
                        [ActivityType.banding.value, ActivityType.additional.value]
                    ),
                    column >= start,
                    column < end,
                )
            )
            if branch_id is not None:
                query = query.filter(OrderModel.branch_id == branch_id)
            for bid, at in query.all():
                days[bid].add(local_date(at))
        return days

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _sale_filters(dr: DateRange, branch_id: Optional[int] = None) -> list:
        """The sales of the range, for the rows and their detail alike.

        A sale is placed by its payment: the amounts are frozen on entering the
        queue, which is gated on registering them. Cancelled orders are out --
        their amounts survive as a record, but nobody keeps that money.
        """
        filters = [
            OrderModel.queued_at >= dr.start,
            OrderModel.queued_at < dr.end,
            OrderModel.status != OrderStatus.cancelled.value,
        ]
        if branch_id is not None:
            filters.append(OrderModel.branch_id == branch_id)
        return filters

    @staticmethod
    def _pending_filters(branch_id: Optional[int] = None) -> list:
        """What is still to collect TODAY: confirmed by the client, not yet paid."""
        filters = [OrderModel.status == OrderStatus.confirmed.value]
        if branch_id is not None:
            filters.append(OrderModel.branch_id == branch_id)
        return filters

    @staticmethod
    def _collection_filters(dr: DateRange, branch_id: Optional[int] = None) -> list:
        """The credit paid back in the range, for the rows and their detail alike.

        Dated by when the money came in, whenever its order was sold. A voided
        payment never happened; a cancelled order's are out with its sale.
        """
        filters = [
            OrderPaymentModel.paid_at >= dr.start,
            OrderPaymentModel.paid_at < dr.end,
            OrderPaymentModel.voided_at.is_(None),
            OrderModel.status != OrderStatus.cancelled.value,
        ]
        if branch_id is not None:
            filters.append(OrderModel.branch_id == branch_id)
        return filters

    def _collected(self, dr: DateRange, branch_id: Optional[int] = None) -> list:
        """``(branch_id, created_by, amount)`` of the credit paid back in range."""
        return (
            self.db.query(
                OrderModel.branch_id,
                OrderModel.created_by,
                func.coalesce(func.sum(OrderPaymentModel.amount), 0.0),
            )
            .join(OrderModel, OrderPaymentModel.order_id == OrderModel.id)
            .filter(*self._collection_filters(dr, branch_id))
            .group_by(OrderModel.branch_id, OrderModel.created_by)
            .all()
        )

    def _outstanding(self, branch_id: Optional[int] = None) -> list:
        """Every order a client still owes on, TODAY, oldest sale first.

        Rows of ``id, branch_id, created_by, queued_at, credit, collected,
        balance``. A snapshot, not a period figure: it is what the office has
        to go and collect, and it comes down with each payment.
        """
        query = self.db.query(
            OrderModel.id,
            OrderModel.branch_id,
            OrderModel.created_by,
            OrderModel.queued_at,
            func.coalesce(OrderModel.payment_credit_amount, 0.0).label("credit"),
            collected_expr().label("collected"),
            credit_balance_expr().label("balance"),
        ).filter(has_balance())
        if branch_id is not None:
            query = query.filter(OrderModel.branch_id == branch_id)
        return query.order_by(OrderModel.queued_at, OrderModel.id).all()

    def _paid_orders(self, dr: DateRange, branch_id: Optional[int] = None) -> list:
        """``(branch_id, created_by, cash, transfer, credit, queued_at)`` of the sales."""
        return (
            self.db.query(
                OrderModel.branch_id,
                OrderModel.created_by,
                OrderModel.payment_cash_amount,
                OrderModel.payment_transfer_amount,
                OrderModel.payment_credit_amount,
                OrderModel.queued_at,
            )
            .filter(*self._sale_filters(dr, branch_id))
            .all()
        )

    def _order_refs(self, order_ids: Iterable[int]) -> dict[int, tuple]:
        """``order_id -> (code, client name, branch_id)``, for a detail's rows."""
        ids = list(order_ids)
        if not ids:
            return {}
        return {
            order_id: (code, customer_name(client), branch_id)
            for order_id, code, branch_id, client in self.db.query(
                OrderModel.id, OrderModel.code, OrderModel.branch_id, ClientModel
            )
            .join(ClientModel, OrderModel.client_id == ClientModel.id)
            .filter(OrderModel.id.in_(ids))
        }

    def _user_or_404(self, user_id: int) -> UserModel:
        user = self.db.get(UserModel, user_id)
        if user is None:
            raise EntityNotFoundError("User", user_id)
        return user

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

    def _work_days(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> tuple[dict[int, dict[date, _WorkDay]], list[OrderCut]]:
        """``branch_id -> day -> its work`` over the range, and the cuts behind it.

        Three readings of the same days: the first and last cutting event (the
        average start and end), the cut's time (the branch's segments, as ONE
        saw: ``work_by_day`` unions them) and the working time its cuts spent in
        a registered pause.
        """
        days: dict[int, dict[date, _WorkDay]] = defaultdict(dict)

        def day_of(bid: int, day: date) -> _WorkDay:
            return days[bid].setdefault(day, _WorkDay(day=day))

        for event in self.production.cut_events(dr.start, dr.end, branch_id):
            day_of(event.branch_id, local_date(event.at)).see(event.at)

        cuts, _ = self.production.order_cuts(dr.start, dr.end, branch_id)
        segments: dict[int, list[WorkSegment]] = defaultdict(list)
        for cut in cuts:
            segments[cut.branch_id] += cut.segments
        for bid, branch_segments in segments.items():
            for day, work in work_by_day(branch_segments, dr.start, dr.end).items():
                workday = day_of(bid, day)
                workday.loading_seconds = work.loading_seconds
                workday.sawing_seconds = work.sawing_seconds

        # A pause counts the schedule's own hours, holidays and all: a day the
        # saw registered nothing on is exactly what a machine down looks like.
        now = datetime.utcnow()
        paused: dict[int, list] = defaultdict(list)
        for pause in self.production.pauses(
            dr.start, dr.end, branch_id, ActivityType.cutting
        ):
            paused[pause.branch_id].append(pause.interval(now))
        for bid, intervals in paused.items():
            per_day = paused_by_day(
                intervals,
                self.production.schedule,
                start=dr.start,
                end=min(dr.end, now),
            )
            for day, took in per_day.items():
                day_of(bid, day).paused_seconds = took
        return days, cuts

    def _production_by_branch(self, dr: DateRange) -> dict[int, _Production]:
        acc: dict[int, _Production] = defaultdict(_Production)
        work_days, _ = self._work_days(dr)
        for bid, days in work_days.items():
            acc[bid].days = list(days.values())
        for board in self.production.processed_boards(dr.start, dr.end):
            acc[board.branch_id].add_board(board)
        for activity in self._closed_activities(dr):
            acc[activity.branch_id].add_closed(activity)
        return acc

    def _efficiency_and_area(self, order_ids: list[int]) -> tuple[float, float]:
        """Area-weighted efficiency (0..100) and area (m²) of the board lines.

        Weighted by area, so a 1-board order does not weigh what a 50-board one
        does; edge-banding lines carry no area and stay out.
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
