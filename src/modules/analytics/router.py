"""Read-only analytics endpoints for the dashboard.

Cohesive endpoints, one per screen block. All return aggregated, chart-ready
payloads wrapped in ``DataResponse[T]``.

The Resumen compares the branches (``/branch-comparison``), Producción reads
``/production`` and Productividad one report per role.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.analytics.constants import Granularity
from src.modules.analytics.dates import DateRange
from src.modules.analytics.performance import PerformanceService, performance_service
from src.modules.analytics.schemas import (
    AttendanceReport,
    BanderOrdersReport,
    BanderReport,
    BottleneckReport,
    BranchComparison,
    OperatorBoardsReport,
    OperatorReport,
    ProductionReport,
    SellerOrdersReport,
    SellerReport,
)
from src.modules.analytics.service import AnalyticsService, analytics_service
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


@router.get("/bottlenecks", response_model=DataResponse[BottleneckReport])
def get_bottlenecks(
    dr: DateRange = Depends(),
    granularity: Granularity = _GRANULARITY_QUERY,
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: AnalyticsService = Depends(analytics_service),
):
    """Bottlenecks: duration per process (avg/median/p90) and when it slows down."""
    return ok(svc.bottlenecks(dr, granularity, branch_id=branch_id))


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


@router.get(
    "/productivity/sellers/{user_id}/orders",
    response_model=DataResponse[SellerOrdersReport],
)
def get_seller_orders(
    user_id: int,
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The orders behind one seller's row: each sale collected in range, with
    its invoice, and what is still to collect today."""
    return ok(svc.seller_orders(user_id, dr, branch_id=branch_id))


@router.get("/productivity/operators", response_model=DataResponse[OperatorReport])
def get_operator_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The cut per operator: pieces, sheets closed, metres and effective hours."""
    return ok(svc.operators(dr, branch_id=branch_id))


@router.get(
    "/productivity/operators/{user_id}/boards",
    response_model=DataResponse[OperatorBoardsReport],
)
def get_operator_boards(
    user_id: int,
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The sheets behind one operator's row: each one they marked a piece on,
    and whether it counts for them (closed by them in range) or why not."""
    return ok(svc.operator_boards(user_id, dr, branch_id=branch_id))


@router.get("/productivity/banders", response_model=DataResponse[BanderReport])
def get_bander_productivity(
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """Banding and additional work per bander (whoever closed it)."""
    return ok(svc.banders(dr, branch_id=branch_id))


@router.get(
    "/productivity/banders/{user_id}/orders",
    response_model=DataResponse[BanderOrdersReport],
)
def get_bander_orders(
    user_id: int,
    dr: DateRange = Depends(),
    branch_id: Optional[int] = _BRANCH_QUERY,
    svc: PerformanceService = Depends(performance_service),
):
    """The work behind one bander's row: each banding and additional work they
    closed in range, with its time or why it has none."""
    return ok(svc.bander_orders(user_id, dr, branch_id=branch_id))


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
