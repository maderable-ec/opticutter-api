"""Read-only aggregations for the dashboard.

The only module that uses SQL aggregation (``func.sum``/``func.count``/``group_by``);
time bucketing and efficiency weighting are done in Python to stay portable and
explicit. The only durable history is the order (optimizations are ephemeral in
Redis), so everything is built on ``orders`` + ``order_lines`` + ``order_status_history``.
"""

from collections import defaultdict
from statistics import median
from typing import Optional

from fastapi import Depends
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session, selectinload

from src.modules.analytics.constants import (
    ACTIVITY_STAGE,
    PENDING_STATUSES,
    REALIZED_STATUSES,
    STAGE_LABELS,
    STAGE_ORDER,
    STATUS_BREAKDOWN_ORDER,
    STATUS_LABELS,
    STATUS_PAIR_TO_STAGE,
    Granularity,
    percentile,
    safe_div,
    status_values,
)
from src.modules.analytics.dates import DateRange, bucket_key, iter_buckets
from src.modules.analytics.schemas import (
    AnalyticsSummary,
    AttendanceDay,
    AttendanceReport,
    BottleneckReport,
    Breakdown,
    BreakdownItem,
    OperationsReport,
    RangeInfo,
    StageDuration,
    StageSeries,
    TimeSeries,
    TimeSeriesData,
    UserAttendance,
    UserProductivity,
    UserProductivityReport,
)
from src.modules.branches.model import BranchModel
from src.modules.orders.model import (
    ActivityType,
    OrderLineModel,
    OrderModel,
    OrderPlacedPieceModel,
    OrderStatus,
)
from src.modules.users.login_event_model import UserLoginEventModel
from src.modules.users.model import UserModel
from src.shared.database import get_db


class AnalyticsService:
    """Computes aggregated metrics over the orders aggregate."""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------ summary
    def summary(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> AnalyticsSummary:
        order_count = self._count(dr, branch_id=branch_id)
        completed_count = self._count(dr, REALIZED_STATUSES, branch_id=branch_id)
        cancelled = self._count(dr, {OrderStatus.cancelled}, branch_id=branch_id)

        realized_revenue = self._revenue(dr, REALIZED_STATUSES, branch_id=branch_id)
        boards = self._boards_consumed(dr, branch_id=branch_id)
        avg_eff, area = self._efficiency_and_area(dr, branch_id=branch_id)
        waste = round(area * (1 - avg_eff / 100), 4)

        active_clients = (
            self.db.query(func.count(distinct(OrderModel.client_id)))
            .filter(*self._range(dr, branch_id))
            .scalar()
        ) or 0

        return AnalyticsSummary(
            range=RangeInfo(date_from=dr.date_from, date_to=dr.date_to),
            total_boards_consumed=boards,
            average_efficiency=avg_eff,
            total_area_cut_m2=area,
            waste_estimate_m2=waste,
            pending_orders_count=self._count(dr, PENDING_STATUSES, branch_id=branch_id),
            cancellation_rate=round(safe_div(cancelled, order_count), 4),
            order_count=order_count,
            realized_revenue=realized_revenue,
            average_ticket=round(safe_div(realized_revenue, completed_count), 2),
            active_clients_count=active_clients,
        )

    # --------------------------------------------------------------- timeseries
    def timeseries(
        self,
        dr: DateRange,
        granularity: Granularity,
        branch_id: Optional[int] = None,
    ) -> TimeSeries:
        buckets = iter_buckets(dr.date_from, dr.date_to, granularity)
        labels = [b.isoformat() for b in buckets]
        index = {b: i for i, b in enumerate(buckets)}
        n = len(buckets)
        revenue = [0.0] * n
        order_count = [0] * n
        boards = [0] * n
        new_clients = [0] * n

        completed = OrderStatus.finished.value
        rows = (
            self.db.query(
                OrderModel.created_at,
                OrderModel.total,
                OrderModel.status,
                OrderModel.total_boards_used,
            )
            .filter(*self._range(dr, branch_id))
            .all()
        )
        for created_at, total, status, tboards in rows:
            i = index.get(bucket_key(created_at.date(), granularity))
            if i is None:
                continue
            order_count[i] += 1
            if status == completed:  # realized revenue/boards
                revenue[i] += total or 0.0
                boards[i] += tboards or 0

        # New clients: first order (MIN created_at) per client over the ENTIRE
        # history; counted in the bucket of that first order if it falls in range.
        # With a branch: "first order" is evaluated within that branch.
        first_orders_q = self.db.query(
            OrderModel.client_id, func.min(OrderModel.created_at)
        )
        if branch_id is not None:
            first_orders_q = first_orders_q.filter(OrderModel.branch_id == branch_id)
        first_orders = first_orders_q.group_by(OrderModel.client_id).all()
        for _, first_dt in first_orders:
            if dr.start <= first_dt < dr.end:
                i = index.get(bucket_key(first_dt.date(), granularity))
                if i is not None:
                    new_clients[i] += 1

        return TimeSeries(
            granularity=granularity,
            buckets=labels,
            series=TimeSeriesData(
                revenue=[round(r, 2) for r in revenue],
                order_count=order_count,
                boards_consumed=boards,
                new_clients=new_clients,
            ),
        )

    # ----------------------------------------------------------- breakdown/status
    def breakdown_status(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> Breakdown:
        rows = (
            self.db.query(
                OrderModel.status,
                func.count(OrderModel.id),
                func.coalesce(func.sum(OrderModel.total), 0.0),
            )
            .filter(*self._range(dr, branch_id))
            .group_by(OrderModel.status)
            .all()
        )
        by_status = {status: (count, rev) for status, count, rev in rows}
        items = [
            BreakdownItem(
                key=st.value,
                label=STATUS_LABELS[st],
                revenue=round(by_status.get(st.value, (0, 0.0))[1], 2),
                order_count=by_status.get(st.value, (0, 0.0))[0],
            )
            # Densify: every LIVE status, even the zero ones. Not the whole enum
            # -- the two legacy statuses only exist to label old history rows and
            # would add two columns permanently at zero.
            for st in STATUS_BREAKDOWN_ORDER
        ]
        return Breakdown(dimension="status", items=items)

    # ----------------------------------------------------------- breakdown/branch
    def breakdown_branch(self, dr: DateRange) -> Breakdown:
        """Breakdown by branch: count and revenue per warehouse (management comparison).

        Densifies with every branch (even ones with no orders in the period) so
        the comparison stays stable.
        """
        rows = (
            self.db.query(
                OrderModel.branch_id,
                func.count(OrderModel.id),
                func.coalesce(func.sum(OrderModel.total), 0.0),
            )
            .filter(*self._range(dr))
            .group_by(OrderModel.branch_id)
            .all()
        )
        by_branch = {bid: (count, rev) for bid, count, rev in rows}
        branches = self.db.query(BranchModel).order_by(BranchModel.id).all()
        items = [
            BreakdownItem(
                key=str(b.id),
                label=b.name,
                revenue=round(by_branch.get(b.id, (0, 0.0))[1], 2),
                order_count=by_branch.get(b.id, (0, 0.0))[0],
            )
            for b in branches
        ]
        return Breakdown(dimension="branch", items=items)

    # ------------------------------------------------------------------ operations
    def operations(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> OperationsReport:
        avg_eff, area = self._efficiency_and_area(dr, branch_id=branch_id)
        waste = round(area * (1 - avg_eff / 100), 4)

        return OperationsReport(
            average_efficiency=avg_eff,
            total_area_cut_m2=area,
            waste_estimate_m2=waste,
        )

    # ----------------------------------------------------------------- bottlenecks
    def bottlenecks(
        self,
        dr: DateRange,
        granularity: Granularity,
        branch_id: Optional[int] = None,
    ) -> BottleneckReport:
        """Duration per process stage: what takes longest (avg/median/p90) and when.

        Four stages come from consecutive pairs in the status history; the three
        work stages (cut, banding, additional) come from the activity rows, which
        is the whole point of having them -- as columns, only the banding was ever
        measurable. The bottleneck is the slowest stage; ``series`` shows in which
        bucket it slows down.
        """
        buckets = iter_buckets(dr.date_from, dr.date_to, granularity)
        labels = [b.isoformat() for b in buckets]
        index = {b: i for i, b in enumerate(buckets)}
        n = len(buckets)

        samples: dict[str, list[float]] = defaultdict(list)
        series_acc: dict[str, list[list[float]]] = {
            key: [[] for _ in range(n)] for key in STAGE_ORDER
        }

        def add_sample(stage: str, hours: float, closed_at) -> None:
            # Always added to the total; added to the bucket only if the close falls within the axis.
            samples[stage].append(hours)
            i = index.get(bucket_key(closed_at.date(), granularity))
            if i is not None:
                series_acc[stage][i].append(hours)

        orders = (
            self.db.query(OrderModel)
            .options(
                selectinload(OrderModel.history), selectinload(OrderModel.activities)
            )
            .filter(*self._range(dr, branch_id))
            .all()
        )
        for o in orders:
            hist = sorted(o.history, key=lambda h: (h.created_at, h.id))
            for prev, cur in zip(hist, hist[1:]):
                if prev.to_status != cur.from_status:
                    continue
                stage = STATUS_PAIR_TO_STAGE.get((cur.from_status, cur.to_status))
                if stage is None:
                    continue
                hours = (cur.created_at - prev.created_at).total_seconds() / 3600.0
                add_sample(stage, hours, cur.created_at)
            # The work itself: parallel activities, not in the history → their
            # own rows. An unfinished one contributes nothing (it has no
            # duration yet), same as a transition that never happened.
            for activity in o.activities:
                if not (activity.started_at and activity.finished_at):
                    continue
                stage = ACTIVITY_STAGE.get(ActivityType(activity.type))
                if stage is None:
                    continue
                hours = (
                    activity.finished_at - activity.started_at
                ).total_seconds() / 3600.0
                add_sample(stage, hours, activity.finished_at)

        stages = [
            StageDuration(
                key=key,
                label=STAGE_LABELS[key],
                avg_hours=round(safe_div(sum(samples[key]), len(samples[key])), 2),
                median_hours=round(median(samples[key]), 2) if samples[key] else 0.0,
                p90_hours=round(percentile(samples[key], 0.9), 2),
                sample_count=len(samples[key]),
            )
            for key in STAGE_ORDER
        ]
        # Slowest first: prioritizes where to intervene.
        stages.sort(key=lambda s: s.median_hours, reverse=True)

        series = [
            StageSeries(
                key=key,
                label=STAGE_LABELS[key],
                avg_hours=[
                    round(safe_div(sum(bucket), len(bucket)), 2)
                    for bucket in series_acc[key]
                ],
            )
            for key in STAGE_ORDER
        ]
        return BottleneckReport(stages=stages, buckets=labels, series=series)

    # ----------------------------------------------------------- user productivity
    def user_productivity(
        self,
        dr: DateRange,
        branch_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> UserProductivityReport:
        """Work and speed per user: cutting, banding, additional and sales work.

        Per-piece cutting is measured by ``cut_at`` within the range; order-level
        metrics (the three activities, sales) by orders created in the range.

        Each activity is credited to whoever FINISHED it, which is also what made
        the cutting hours honest: they used to be read off the status history and
        kept the LAST entry into ``cutting``, so an admin rollback silently reset
        the clock of whoever had been cutting since morning.
        """
        acc: dict[int, dict] = defaultdict(
            lambda: {
                "pieces_cut": 0,
                "area_cut_m2": 0.0,
                "orders_cut": set(),
                "cutting_hours": 0.0,
                "boards_cut": 0,
                "orders_banded": 0,
                "banding_hours": 0.0,
                "orders_additional": 0,
                "additional_hours": 0.0,
                "orders_created": 0,
                "revenue_generated": 0.0,
            }
        )

        # 1) Per physical piece marked cut in the range → operator who cut it.
        piece_q = self.db.query(
            OrderPlacedPieceModel.cut_by,
            OrderPlacedPieceModel.width,
            OrderPlacedPieceModel.height,
            OrderPlacedPieceModel.order_id,
        ).filter(
            OrderPlacedPieceModel.cut_by.isnot(None),
            OrderPlacedPieceModel.cut_at >= dr.start,
            OrderPlacedPieceModel.cut_at < dr.end,
        )
        if branch_id is not None:
            piece_q = piece_q.join(
                OrderModel, OrderPlacedPieceModel.order_id == OrderModel.id
            ).filter(OrderModel.branch_id == branch_id)
        for cut_by, width, height, order_id in piece_q.all():
            a = acc[cut_by]
            a["pieces_cut"] += 1
            a["area_cut_m2"] += (width * height) / 1_000_000.0
            a["orders_cut"].add(order_id)

        # 2-4) Order-level metrics (order created in the range).
        realized = status_values(REALIZED_STATUSES)
        orders = (
            self.db.query(OrderModel)
            .options(selectinload(OrderModel.activities))
            .filter(*self._range(dr, branch_id))
            .all()
        )
        for o in orders:
            # Each activity → whoever finished it, with its own duration.
            for activity in o.activities:
                if activity.finished_by is None:
                    continue
                a = acc[activity.finished_by]
                hours = 0.0
                if activity.started_at and activity.finished_at:
                    hours = (
                        activity.finished_at - activity.started_at
                    ).total_seconds() / 3600.0
                kind = ActivityType(activity.type)
                if kind is ActivityType.cutting:
                    a["cutting_hours"] += hours
                    a["boards_cut"] += o.total_boards_used or 0
                elif kind is ActivityType.banding:
                    a["orders_banded"] += 1
                    a["banding_hours"] += hours
                else:
                    a["orders_additional"] += 1
                    a["additional_hours"] += hours
            # Sales work → creating vendedor.
            if o.created_by is not None:
                a = acc[o.created_by]
                a["orders_created"] += 1
                if o.status in realized:
                    a["revenue_generated"] += o.total or 0.0

        return UserProductivityReport(users=self._productivity_rows(acc, role))

    # --------------------------------------------------------------------- attendance
    def attendance(
        self,
        dr: DateRange,
        branch_id: Optional[int] = None,
        role: Optional[str] = None,
    ) -> AttendanceReport:
        """First login per user and day (clock-in time reference)."""
        rows = (
            self.db.query(UserLoginEventModel.user_id, UserLoginEventModel.created_at)
            .filter(
                UserLoginEventModel.created_at >= dr.start,
                UserLoginEventModel.created_at < dr.end,
            )
            .all()
        )
        per_user_day: dict[int, dict] = defaultdict(lambda: defaultdict(list))
        for user_id, created_at in rows:
            per_user_day[user_id][created_at.date()].append(created_at)
        if not per_user_day:
            return AttendanceReport(users=[])

        users, branches = self._users_and_branches(per_user_day.keys())
        result = []
        for uid, days_map in per_user_day.items():
            user = users.get(uid)
            if user is None or not self._matches(user, branch_id, role):
                continue
            days = [
                AttendanceDay(date=d, first_login_at=min(times), login_count=len(times))
                for d, times in sorted(days_map.items())
            ]
            result.append(
                UserAttendance(
                    user_id=uid,
                    full_name=user.full_name or "",
                    role=user.role,
                    branch_name=branches.get(user.branch_id),
                    days=days,
                )
            )
        result.sort(key=lambda u: u.full_name)
        return AttendanceReport(users=result)

    # --------------------------------------------------------------------- helpers
    def _range(self, dr: DateRange, branch_id: Optional[int] = None) -> list:
        """Half-open ``[start, end)`` filter on ``created_at`` (+ branch).

        When ``branch_id`` is not ``None``, restricts to that branch: this lets
        the manager review the performance of a specific warehouse (``None`` = all).
        """
        filters = [OrderModel.created_at >= dr.start, OrderModel.created_at < dr.end]
        if branch_id is not None:
            filters.append(OrderModel.branch_id == branch_id)
        return filters

    def _count(
        self, dr: DateRange, statuses=None, branch_id: Optional[int] = None
    ) -> int:
        query = self.db.query(func.count(OrderModel.id)).filter(
            *self._range(dr, branch_id)
        )
        if statuses is not None:
            query = query.filter(OrderModel.status.in_(status_values(statuses)))
        return query.scalar() or 0

    def _revenue(
        self, dr: DateRange, statuses, branch_id: Optional[int] = None
    ) -> float:
        val = (
            self.db.query(func.coalesce(func.sum(OrderModel.total), 0.0))
            .filter(*self._range(dr, branch_id))
            .filter(OrderModel.status.in_(status_values(statuses)))
            .scalar()
        )
        return round(val or 0.0, 2)

    def _boards_consumed(self, dr: DateRange, branch_id: Optional[int] = None) -> int:
        """Boards from completed orders (realized production)."""
        val = (
            self.db.query(func.coalesce(func.sum(OrderModel.total_boards_used), 0))
            .filter(*self._range(dr, branch_id))
            .filter(OrderModel.status.in_(status_values(REALIZED_STATUSES)))
            .scalar()
        )
        return int(val or 0)

    def _efficiency_and_area(
        self, dr: DateRange, branch_id: Optional[int] = None
    ) -> tuple[float, float]:
        """Area-weighted efficiency (0..100) and total area (m²) of board lines.

        Excludes edge-banding lines (null ``total_area_m2``/``avg_efficiency``). The
        average is weighted by area: a 1-board order doesn't weigh the same as a 50-board one.
        """
        rows = (
            self.db.query(OrderLineModel.avg_efficiency, OrderLineModel.total_area_m2)
            .join(OrderModel, OrderLineModel.order_id == OrderModel.id)
            .filter(*self._range(dr, branch_id))
            .filter(OrderModel.status.in_(status_values(REALIZED_STATUSES)))
            .filter(OrderLineModel.total_area_m2.isnot(None))
            .filter(OrderLineModel.avg_efficiency.isnot(None))
            .all()
        )
        total_area = sum(area for _, area in rows)
        if not total_area:
            return 0.0, 0.0
        weighted = sum(eff * area for eff, area in rows) / total_area
        return round(weighted, 2), round(total_area, 4)

    def _productivity_rows(
        self, acc: dict[int, dict], role: Optional[str]
    ) -> list[UserProductivity]:
        """Projects the per-user accumulator to rows, filtering by role."""
        if not acc:
            return []
        users, branches = self._users_and_branches(acc.keys())
        rows = []
        for uid, a in acc.items():
            user = users.get(uid)
            if user is None or (role is not None and user.role != role):
                continue
            rows.append(
                UserProductivity(
                    user_id=uid,
                    full_name=user.full_name or "",
                    role=user.role,
                    branch_name=branches.get(user.branch_id),
                    pieces_cut=a["pieces_cut"],
                    area_cut_m2=round(a["area_cut_m2"], 4),
                    orders_cut=len(a["orders_cut"]),
                    cutting_hours=round(a["cutting_hours"], 2),
                    pieces_per_hour=round(
                        safe_div(a["pieces_cut"], a["cutting_hours"]), 2
                    ),
                    boards_cut=a["boards_cut"],
                    orders_banded=a["orders_banded"],
                    banding_hours=round(a["banding_hours"], 2),
                    orders_additional=a["orders_additional"],
                    additional_hours=round(a["additional_hours"], 2),
                    orders_created=a["orders_created"],
                    revenue_generated=round(a["revenue_generated"], 2),
                )
            )
        # Most work first (cutting + banding + additional + sales).
        rows.sort(
            key=lambda r: r.pieces_cut
            + r.orders_banded
            + r.orders_additional
            + r.orders_created,
            reverse=True,
        )
        return rows

    def _users_and_branches(self, user_ids) -> tuple[dict, dict]:
        """Loads users by id and the ``branch_id → name`` map in two queries."""
        ids = list(user_ids)
        users = {
            u.id: u
            for u in self.db.query(UserModel).filter(UserModel.id.in_(ids)).all()
        }
        branches = {b.id: b.name for b in self.db.query(BranchModel).all()}
        return users, branches

    @staticmethod
    def _matches(
        user: UserModel, branch_id: Optional[int], role: Optional[str]
    ) -> bool:
        """Does the user pass the optional branch and role filters?"""
        if branch_id is not None and user.branch_id != branch_id:
            return False
        if role is not None and user.role != role:
            return False
        return True


def analytics_service(db: Session = Depends(get_db)) -> AnalyticsService:
    """``AnalyticsService`` provider for injection into routes."""
    return AnalyticsService(db)
