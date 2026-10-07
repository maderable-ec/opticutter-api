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
    roles: List[str]
    # DEPRECATED: the primary role (first of ``roles``); removed with migration 016.
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
    roles: List[str]
    # DEPRECATED: the primary role (first of ``roles``); removed with migration 016.
    role: str
    branch_name: Optional[str]
    days: List[AttendanceDay]


class AttendanceReport(CamelModel):
    """Clock-in time per user and day."""

    users: List[UserAttendance]


# ------------------------------------------------------------ branch comparison
class SalesFigures(CamelModel):
    """Collected sales: the payment registered on entering the queue (``queuedAt``).

    Two methods, as the office reads them: ``cash`` is money already received
    (cash + bank transfer), ``credit`` is owed. Cancelled orders are out.
    """

    cash: float
    credit: float
    total: float
    paid_orders: int


class OrderFigures(CamelModel):
    """Orders that came in (created) and that the shop closed (``finished``) in range."""

    entered: int
    finished: int


class ProductionFigures(CamelModel):
    """The saw's work in range, read off the cutting events.

    ``boards`` weighs a whole board 1, a half 0.5 and a retazo 0; the cut metres
    are the saw travel of every sheet finished, and the banded metres the net
    tape of every banding closed (no waste factor). Hours split each business day's span
    into effective and paused by the idle gap. ``days_worked`` and the average
    start/end (minutes after the business's midnight) count only days with some
    effective time: a lone event is not a workday. Rates are 0 without hours.
    """

    boards: float
    cut_linear_m: float
    banded_linear_m: float
    effective_hours: float
    paused_hours: float
    boards_per_hour: float
    meters_per_hour: float
    days_worked: int
    average_start_minute: int
    average_end_minute: int


class BranchFigures(CamelModel):
    """Everything the comparison shows for one branch (or the total)."""

    branch_id: Optional[int]  # null on the total
    branch_name: str
    sales: SalesFigures
    orders: OrderFigures
    production: ProductionFigures


class BranchComparison(CamelModel):
    """Branch against branch for the period: one entry per branch plus the total.

    Every active branch is listed, zeros included, so the columns stay put. The
    total recomputes its rates from its own sums.
    """

    range: RangeInfo
    idle_minutes: int
    branches: List[BranchFigures]
    total: BranchFigures


# ------------------------------------------------------------------- production
class ProductionDay(CamelModel):
    """One business day of one branch."""

    date: date
    branch_id: int
    branch_name: str
    first_event_at: Optional[datetime]  # null: boards/orders but no event that day
    last_event_at: Optional[datetime]
    effective_hours: float
    paused_hours: float
    boards: float
    cut_linear_m: float
    banded_linear_m: float  # bandings closed that day
    orders_finished: int
    boards_per_hour: float
    meters_per_hour: float


class ProductionStop(CamelModel):
    """A gap longer than the idle gap inside a business day."""

    branch_id: int
    branch_name: str
    started_at: datetime
    ended_at: datetime
    minutes: int


class BranchMaterial(CamelModel):
    """Material use of the orders a branch FINISHED in range (area-weighted)."""

    branch_id: int
    branch_name: str
    average_efficiency: float  # 0..100
    area_cut_m2: float
    waste_estimate_m2: float


class ProductionReport(CamelModel):
    """The production tab: days (newest first), the longest stops, material."""

    range: RangeInfo
    idle_minutes: int
    days: List[ProductionDay]
    stops: List[ProductionStop]
    material: List[BranchMaterial]


# -------------------------------------------------------- productivity by role
class SellerFigures(CamelModel):
    """A seller's collected sales in range, plus what is still to collect TODAY.

    ``pending_*`` are the seller's orders in ``confirmed`` right now: confirmed
    by the client, payment not yet registered. A snapshot, not a period figure.
    """

    paid_orders: int
    cash: float
    credit: float
    total: float
    average_ticket: float
    pending_count: int
    pending_amount: float


class SellerRow(SellerFigures):
    user_id: Optional[int]  # null: orders nobody owns (no quote behind them)
    full_name: str
    branch_name: Optional[str]


class SellerReport(CamelModel):
    range: RangeInfo
    sellers: List[SellerRow]
    total: SellerFigures


class OperatorFigures(CamelModel):
    """An operator's cut in range, by the same rules as the production tab.

    A sheet is credited to whoever marked its last piece; the hours are the
    workday rule applied to the operator's OWN events.
    """

    pieces_cut: int
    boards: float
    cut_linear_m: float
    effective_hours: float
    boards_per_hour: float
    meters_per_hour: float
    orders_cut: int


class OperatorRow(OperatorFigures):
    user_id: int
    full_name: str
    branch_name: Optional[str]


class OperatorReport(CamelModel):
    range: RangeInfo
    idle_minutes: int
    operators: List[OperatorRow]
    total: OperatorFigures


class BanderFigures(CamelModel):
    """Banding and additional work closed in range, credited to who closed it.

    Hours run from the activity's start to its close: the banding marks no
    pieces, so its stops cannot be told apart the way the cut's can. The metres
    are the order's NET tape (no waste factor), special edges included.
    """

    orders_banded: int
    banding_hours: float
    banded_linear_m: float
    banding_meters_per_hour: float
    average_banding_hours: float
    orders_additional: int
    additional_hours: float
    average_additional_hours: float


class BanderRow(BanderFigures):
    user_id: int
    full_name: str
    branch_name: Optional[str]


class BanderReport(CamelModel):
    range: RangeInfo
    banders: List[BanderRow]
    total: BanderFigures
