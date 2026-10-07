"""Bottlenecks and attendance: the two reports that are not branch performance.

The branch comparison, the production tab and productivity by role live in
``performance.py``. Time bucketing is done in Python to stay portable and
explicit.
"""

from collections import defaultdict
from statistics import median
from typing import Optional

from fastapi import Depends
from sqlalchemy.orm import Session, selectinload

from src.modules.analytics.constants import (
    ACTIVITY_STAGE,
    STAGE_LABELS,
    STAGE_ORDER,
    STATUS_PAIR_TO_STAGE,
    Granularity,
    percentile,
    safe_div,
)
from src.modules.analytics.dates import DateRange, bucket_key, iter_buckets
from src.modules.analytics.schemas import (
    AttendanceDay,
    AttendanceReport,
    BottleneckReport,
    StageDuration,
    StageSeries,
    UserAttendance,
)
from src.modules.branches.model import BranchModel
from src.modules.orders.model import ActivityType, OrderModel
from src.modules.orders.production import clocked_seconds
from src.modules.users.login_event_model import UserLoginEventModel
from src.modules.users.model import UserModel
from src.shared.business_time import local_date
from src.shared.database import get_db


class AnalyticsService:
    """Computes aggregated metrics over the orders aggregate."""

    def __init__(self, db: Session):
        self.db = db

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
            i = index.get(bucket_key(local_date(closed_at), granularity))
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
            # duration yet), same as a transition that never happened; nor
            # does one started and closed together after the work: its
            # seconds measure the tap, and would drag the median to zero.
            for activity in o.activities:
                seconds = clocked_seconds(activity.started_at, activity.finished_at)
                if seconds is None:
                    continue
                stage = ACTIVITY_STAGE.get(ActivityType(activity.type))
                if stage is None:
                    continue
                add_sample(stage, seconds / 3600.0, activity.finished_at)

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
            per_user_day[user_id][local_date(created_at)].append(created_at)
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
                    roles=user.roles,
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
        """Does the user pass the optional branch and role filters?

        The role filter matches a user HOLDING that role, among others.
        """
        if branch_id is not None and user.branch_id != branch_id:
            return False
        if role is not None and role not in user.roles:
            return False
        return True


def analytics_service(db: Session = Depends(get_db)) -> AnalyticsService:
    """``AnalyticsService`` provider for injection into routes."""
    return AnalyticsService(db)
