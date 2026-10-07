"""Analytics response contracts (chart-ready, camelCase via ``CamelModel``).

Conventions: no numeric field is optional (empty range → zeros, never nulls);
time series are parallel arrays over the same ``buckets`` axis.
"""

from datetime import date, datetime
from typing import List, Literal, Optional

from src.modules.orders.production import SheetCredit, SheetKind
from src.shared.schemas import CamelModel


class RangeInfo(CamelModel):
    """Window effectively applied (echo of the resolved defaults)."""

    date_from: date
    date_to: date


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
    tape of every banding closed (no waste factor). ``orders_banded`` and
    ``orders_additional`` count the bandings and additional works closed in
    range, dated by the close. Hours split each business day's span
    into effective and paused by the idle gap. ``days_worked`` and the average
    start/end (minutes after the business's midnight) count only days with some
    effective time: a lone event is not a workday. Rates are 0 without hours.
    """

    boards: float
    cut_linear_m: float
    banded_linear_m: float
    orders_banded: int
    orders_additional: int
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
    orders_banded: int  # bandings closed that day
    orders_additional: int  # additional works closed that day
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


class SellerOrder(CamelModel):
    """One sale of the seller in range: an order whose payment landed in it.

    ``paid_at`` (entering the queue) dates the sale, not ``created_at``: an
    order confirmed in September and paid in October is an October sale.
    ``invoice`` is the accounting system's number, to check the figure there.
    """

    order_id: int
    order_code: Optional[str]
    client_name: str
    branch_name: str
    created_at: datetime
    paid_at: datetime
    day: date  # the business day of the payment
    cash: float
    transfer: float
    credit: float
    total: float  # cash + transfer + credit
    invoice: Optional[str]


class SellerPendingOrder(CamelModel):
    """An order of the seller confirmed by the client and not yet paid, today."""

    order_id: int
    order_code: Optional[str]
    client_name: str
    branch_name: str
    confirmed_at: Optional[datetime]
    total: float


class SellerOrdersReport(CamelModel):
    """The orders behind one seller's row. ``figures`` IS the row: it adds up
    from ``paid`` (the sales) and ``pending`` (what is still to collect)."""

    range: RangeInfo
    user_id: int
    full_name: str
    figures: SellerFigures
    paid: List[SellerOrder]
    pending: List[SellerPendingOrder]


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
    # null: the sheets and pieces of users deleted since (``cut_by`` went NULL),
    # one "Sin usuario" row so the total still matches the branch comparison.
    user_id: Optional[int]
    full_name: str
    branch_name: Optional[str]


class OperatorReport(CamelModel):
    range: RangeInfo
    idle_minutes: int
    operators: List[OperatorRow]
    total: OperatorFigures


class OperatorBoard(CamelModel):
    """One sheet the operator marked a piece on in range, and whether it counts.

    ``status``: ``credited`` (they marked its last piece, inside the range: it
    is in their ``boards``), ``credited_to_other`` (somebody else did, see
    ``closedBy``), ``incomplete`` (a piece is still unmarked: it counts for
    nobody yet) or ``outside_range`` (they closed it after ``to``).
    """

    board_id: int
    order_id: int
    order_code: Optional[str]
    client_name: str
    branch_name: str
    sheet_number: int
    material_name: Optional[str]  # null: a retazo or a hand-measured sheet
    width: float
    height: float
    kind: SheetKind
    weight: float  # what it adds to ``boards`` when credited: 1, 0.5 or 0
    # The business day it belongs to: of its last mark, or of the operator's
    # last mark while it is incomplete.
    day: date
    pieces_total: int
    pieces_mine: int  # marked by this operator, whenever
    pieces_mine_in_range: int  # ...inside the range: they add up to ``piecesCut``
    pieces_by_others: int
    pieces_pending: int
    other_cutters: List[str]  # who else marked pieces on it (frozen labels)
    my_last_cut_at: datetime
    done_at: Optional[datetime]  # the last mark; null while incomplete
    closed_by: Optional[str]  # who made the last mark (frozen label)
    status: SheetCredit


class OperatorBoardsReport(CamelModel):
    """The ledger behind one operator's row: every sheet they touched in range.

    ``boards`` and ``pieces_cut`` are the row's own figures, and add up from
    ``sheets``: the weights of the credited ones, the in-range marks of all.
    """

    range: RangeInfo
    user_id: int
    full_name: str
    boards: float
    credited_count: int
    pieces_cut: int
    sheets: List[OperatorBoard]


class BanderFigures(CamelModel):
    """Banding and additional work closed in range, credited to who closed it.

    Hours run from the activity's start to its close: the banding marks no
    pieces, so its stops cannot be told apart the way the cut's can. The metres
    are the order's NET tape (no waste factor), special edges included.

    Every closed activity counts as work done (orders, metres), but only the
    CLOCKED ones carry time: a start and a close under a minute apart were
    registered together after the work (``*_unclocked``), and their seconds
    would read as a job done in no time. Hours, averages and the metres per
    hour are over the clocked ones only.
    """

    orders_banded: int
    orders_banded_unclocked: int
    banding_hours: float
    banded_linear_m: float
    banding_meters_per_hour: float
    average_banding_hours: float
    orders_additional: int
    orders_additional_unclocked: int
    additional_hours: float
    average_additional_hours: float


class BanderRow(BanderFigures):
    user_id: Optional[int]  # null: closed by a user deleted since ("Sin usuario")
    full_name: str
    branch_name: Optional[str]


class BanderReport(CamelModel):
    range: RangeInfo
    banders: List[BanderRow]
    total: BanderFigures


class BanderOrder(CamelModel):
    """One banding or additional work the bander closed in range.

    ``hours`` is null when it was not clocked: started and closed under a
    minute apart, registered after the work. It still counts as work done.
    """

    order_id: int
    order_code: Optional[str]
    client_name: str
    branch_name: str
    kind: Literal["banding", "additional"]
    started_at: Optional[datetime]
    finished_at: datetime
    day: date  # the business day of the close
    hours: Optional[float]
    banded_linear_m: float  # the order's net tape; 0 for the additional work


class BanderOrdersReport(CamelModel):
    """The work behind one bander's row. ``figures`` IS the row: it adds up
    from ``orders``."""

    range: RangeInfo
    user_id: int
    full_name: str
    figures: BanderFigures
    orders: List[BanderOrder]
