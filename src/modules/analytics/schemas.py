"""Analytics response contracts (chart-ready, camelCase via ``CamelModel``).

Conventions: no numeric field is optional (empty range → zeros, never nulls);
time series are parallel arrays over the same ``buckets`` axis.
"""

from datetime import date, datetime
from typing import List, Optional

from src.modules.analytics.constants import Granularity
from src.shared.schemas import CamelModel


class RangeInfo(CamelModel):
    """Window effectively applied (echo of the resolved defaults)."""

    date_from: date
    date_to: date


class AnalyticsSummary(CamelModel):
    """KPI cards: operations, pipeline and base trend for the period."""

    range: RangeInfo
    # Operations (over completed orders).
    total_boards_consumed: int
    average_efficiency: float  # area-weighted, 0..100
    total_area_cut_m2: float
    waste_estimate_m2: float
    # Status / pipeline.
    pending_orders_count: int
    cancellation_rate: float  # 0..1
    # Base trend.
    order_count: int
    realized_revenue: float
    average_ticket: float
    active_clients_count: int


class TimeSeriesData(CamelModel):
    """Parallel series aligned to ``TimeSeries``'s ``buckets`` axis."""

    revenue: List[float]  # realized revenue per bucket
    order_count: List[int]
    boards_consumed: List[int]
    new_clients: List[int]


class TimeSeries(CamelModel):
    """Time trends with a dense axis (gaps filled with zero)."""

    granularity: Granularity
    buckets: List[str]  # ISO bucket dates (x axis)
    series: TimeSeriesData


class BreakdownItem(CamelModel):
    """One breakdown category with its metrics."""

    key: str
    label: str
    revenue: float
    order_count: int


class Breakdown(CamelModel):
    """Categorical breakdown (e.g. status funnel)."""

    dimension: str
    items: List[BreakdownItem]


class OperationsReport(CamelModel):
    """Material efficiency of the orders (the lifecycle lives in ``/bottlenecks``)."""

    average_efficiency: float
    total_area_cut_m2: float
    waste_estimate_m2: float


# ------------------------------------------------------------------ bottlenecks
class StageDuration(CamelModel):
    """Aggregated duration of a process stage (to find the bottleneck)."""

    key: str
    label: str
    avg_hours: float
    median_hours: float
    p90_hours: float  # slow tail: what the average hides
    sample_count: int


class StageSeries(CamelModel):
    """Average duration of a stage per bucket (when it slows down); zeros in gaps."""

    key: str
    label: str
    avg_hours: List[float]  # parallel to ``BottleneckReport``'s ``buckets``


class BottleneckReport(CamelModel):
    """Which process takes longest (``stages``, slowest first) and when (``series``)."""

    stages: List[StageDuration]
    buckets: List[str]
    series: List[StageSeries]


# ----------------------------------------------------------- user productivity
class UserProductivity(CamelModel):
    """Work and speed of a user in the period (not applicable → 0, never null)."""

    user_id: int
    full_name: str
    role: str
    branch_name: Optional[str]
    # Cutting (operador).
    pieces_cut: int
    area_cut_m2: float
    orders_cut: int
    cutting_hours: float
    pieces_per_hour: float  # throughput
    boards_cut: int
    # Banding (canteador).
    orders_banded: int
    banding_hours: float
    # Additional work -- perforación, armado, bisagras (canteador too).
    orders_additional: int = 0
    additional_hours: float = 0.0
    # Sales (vendedor).
    orders_created: int
    revenue_generated: float


class UserProductivityReport(CamelModel):
    """Productivity per user (workshop + sales)."""

    users: List[UserProductivity]


# --------------------------------------------------------------------- attendance
class AttendanceDay(CamelModel):
    """A user's attendance on one day: first login (clock-in time) and count."""

    date: date
    first_login_at: datetime
    login_count: int


class UserAttendance(CamelModel):
    """Days with a login for a user in the range (clock-in time reference)."""

    user_id: int
    full_name: str
    role: str
    branch_name: Optional[str]
    days: List[AttendanceDay]


class AttendanceReport(CamelModel):
    """Clock-in time per user and day."""

    users: List[UserAttendance]
