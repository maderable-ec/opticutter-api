"""Read-only analytics endpoints for the dashboard.

Cohesive endpoints, one per chart family. All return aggregated,
chart-ready payloads wrapped in ``DataResponse[T]``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.analytics.constants import Granularity
from src.modules.analytics.dates import DateRange
from src.modules.analytics.schemas import (
    AnalyticsSummary,
    AttendanceReport,
    BottleneckReport,
    Breakdown,
    OperationsReport,
    TimeSeries,
    UserProductivityReport,
)
from src.modules.analytics.service import AnalyticsService, analytics_service
from src.modules.inventory.schemas import LowStockReport
from src.modules.inventory.service import StockService, stock_service
from src.modules.products.model import ProductType
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

_PRODUCT_TYPE_QUERY = Query(
    default=None,
    alias="type",
    description="Restricts the low-stock report to one product type (empty = all)",
)

_ROLE_QUERY = Query(default=None, description="Filters by role (empty = all)")


@router.get("/summary", response_model=DataResponse[AnalyticsSummary])
def get_summary(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """KPI cards for the period: operations, pipeline and base trend."""
    return ok(svc.summary(dr, branch_id=branch_id))


@router.get("/timeseries", response_model=DataResponse[TimeSeries])
def get_timeseries(
    dr: DateRange = Depends(),
    granularity: Granularity = _GRANULARITY_QUERY,
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Time trends (dense axis, gaps filled with zero)."""
    return ok(svc.timeseries(dr, granularity, branch_id=branch_id))


@router.get("/breakdown/status", response_model=DataResponse[Breakdown])
def get_breakdown_status(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Status funnel: count and revenue per status (every status, incl. zero)."""
    return ok(svc.breakdown_status(dr, branch_id=branch_id))


@router.get("/breakdown/branch", response_model=DataResponse[Breakdown])
def get_breakdown_branch(
    dr: DateRange = Depends(),
    svc: AnalyticsService = Depends(analytics_service),
):
    """Branch comparison: count and revenue per warehouse (management review)."""
    return ok(svc.breakdown_branch(dr))


@router.get("/operations", response_model=DataResponse[OperationsReport])
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


@router.get("/users", response_model=DataResponse[UserProductivityReport])
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


@router.get("/low-stock", response_model=DataResponse[LowStockReport])
def get_low_stock(
    branch_id: Optional[int] = _BRANCH_QUERY,
    product_type: Optional[ProductType] = _PRODUCT_TYPE_QUERY,
    svc: StockService = Depends(stock_service),
):
    """Boards and edge bandings below their configured threshold, per branch.

    The one endpoint here that takes no ``DateRange``, and deliberately: stock
    is a state right now, not a metric over a window — asking for "low stock
    last March" has no answer the vendor's system could give.

    Unpaginated like the rest of this module. The worst case is bounded by the
    catalog (a few hundred rows with everything at zero), and the point of the
    screen is to be read top to bottom in buying order: branch, then type, then
    emptiest first.
    """
    return ok(
        svc.low_stock(
            branch_id=branch_id,
            product_type=product_type.value if product_type else None,
        )
    )
