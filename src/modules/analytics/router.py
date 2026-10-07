"""Read-only analytics endpoints for the dashboard.

Cohesive endpoints, one per screen block. All return aggregated, chart-ready
payloads wrapped in ``DataResponse[T]``.

The Resumen compares the branches (``/branch-comparison``), Producción reads
``/production`` and Productividad one report per role. The first generation of
the dashboard (``summary``, ``timeseries``, ``breakdown/*``, ``operations`` and
the all-roles ``productivity``) filtered everything by the order's creation
date; it answers out of the schema for the previous web build and goes in the
release after, with the other aliases.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.analytics.constants import Granularity
from src.modules.analytics.dates import DateRange
from src.modules.analytics.performance import PerformanceService, performance_service
from src.modules.analytics.schemas import (
    AnalyticsSummary,
    AttendanceReport,
    BanderReport,
    BottleneckReport,
    BranchComparison,
    Breakdown,
    OperationsReport,
    OperatorReport,
    ProductionReport,
    SellerReport,
    TimeSeries,
    UserProductivityReport,
)
from src.modules.analytics.service import AnalyticsService, analytics_service
from src.modules.inventory.router import get_low_stock
from src.modules.inventory.schemas import LowStockReport
from src.modules.users.dependencies import require_permission
from src.modules.users.enums import UserRole
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

# Dashboard analytics: "administrador" only (RESOURCE_ROLES["analytics"]). The
# admin is global, so ``branchId`` is an OPTIONAL filter to review one warehouse.
router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("analytics"))],
)

_BRANCH_QUERY = Query(
    default=None,
    alias="branchId",
    description="Restricts metrics to a branch (empty = all)",
)

_GRANULARITY_QUERY = Query(
    Granularity.day, description="Bucket size: day | week | month"
)

_ROLE_QUERY = Query(default=None, description="Users holding this role (empty = all)")


@router.get(
    "/summary", response_model=DataResponse[AnalyticsSummary], include_in_schema=False
)
def get_summary(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """KPI cards for the period: operations, pipeline and base trend."""
    return ok(svc.summary(dr, branch_id=branch_id))


@router.get(
    "/timeseries", response_model=DataResponse[TimeSeries], include_in_schema=False
)
def get_timeseries(
    dr: DateRange = Depends(),
    granularity: Granularity = _GRANULARITY_QUERY,
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Time trends (dense axis, gaps filled with zero)."""
    return ok(svc.timeseries(dr, granularity, branch_id=branch_id))


@router.get(
    "/breakdown/status", response_model=DataResponse[Breakdown], include_in_schema=False
)
def get_breakdown_status(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Status funnel: count and revenue per status (every status, incl. zero)."""
    return ok(svc.breakdown_status(dr, branch_id=branch_id))


@router.get(
    "/breakdown/branch", response_model=DataResponse[Breakdown], include_in_schema=False
)
def get_breakdown_branch(
    dr: DateRange = Depends(),
    svc: AnalyticsService = Depends(analytics_service),
):
    """Branch comparison: count and revenue per warehouse (management review)."""
    return ok(svc.breakdown_branch(dr))


@router.get(
    "/operations",
    response_model=DataResponse[OperationsReport],
    include_in_schema=False,
)
def get_operations(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Material efficiency (area-weighted) and waste."""
    return ok(svc.operations(dr, branch_id=branch_id))


@router.get("/bottlenecks", response_model=DataResponse[BottleneckReport])
def get_bottlenecks(
    dr: DateRange = Depends(),
    granularity: Granularity = _GRANULARITY_QUERY,
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Bottlenecks: duration per process (avg/median/p90) and when it slows down."""
    return ok(svc.bottlenecks(dr, granularity, branch_id=branch_id))


@router.get(
    "/productivity",
    response_model=DataResponse[UserProductivityReport],
    include_in_schema=False,
)
# Deprecated alias: the name said user admin, not productivity. Kept, out of the
# schema, for the dashboards still open on the previous web build; drop it with
# the other aliases in the release after.
@router.get(
    "/users",
    response_model=DataResponse[UserProductivityReport],
    include_in_schema=False,
)
def get_user_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    role: Optional[UserRole] = _ROLE_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Productivity per user: cutting, banding and sales work."""
    return ok(
        svc.user_productivity(
            dr, branch_id=branch_id, role=role.value if role else None
        )
    )


@router.get("/branch-comparison", response_model=DataResponse[BranchComparison])
def get_branch_comparison(
    dr: DateRange = Depends(),
    svc: PerformanceService = Depends(performance_service),
):
    """Branch against branch: sales, orders and production, plus the total.

    Every figure is placed in the period by when it happened (the payment, the
    last piece of a sheet, the closing), on the business's days.
    """
    return ok(svc.branch_comparison(dr))


@router.get("/production", response_model=DataResponse[ProductionReport])
def get_production(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The workday per branch and day, the longest stops and material use."""
    return ok(svc.production_report(dr, branch_id=branch_id))


@router.get("/productivity/sellers", response_model=DataResponse[SellerReport])
def get_seller_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """Collected sales per seller (who raised the quote) and what is to collect."""
    return ok(svc.sellers(dr, branch_id=branch_id))


@router.get("/productivity/operators", response_model=DataResponse[OperatorReport])
def get_operator_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The cut per operator: pieces, sheets closed, metres and effective hours."""
    return ok(svc.operators(dr, branch_id=branch_id))


@router.get("/productivity/banders", response_model=DataResponse[BanderReport])
def get_bander_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """Banding and additional work per bander (whoever closed it)."""
    return ok(svc.banders(dr, branch_id=branch_id))


@router.get("/attendance", response_model=DataResponse[AttendanceReport])
def get_attendance(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    role: Optional[UserRole] = _ROLE_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """First login time per day and user (clock-in time reference)."""
    return ok(
        svc.attendance(dr, branch_id=branch_id, role=role.value if role else None)
    )


# Deprecated alias: the low-stock report moved to ``GET /inventory/low-stock``,
# the domain it belongs to. Same roles here (``analytics`` is admin only, like
# ``inventory:low-stock``). Drop it with the other aliases in the release after.
router.add_api_route(
    "/low-stock",
    get_low_stock,
    methods=["GET"],
    response_model=DataResponse[LowStockReport],
    include_in_schema=False,
)
