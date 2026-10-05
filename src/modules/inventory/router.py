"""Stock endpoints: the question a seller asks while quoting, and the report the
administrator reorders from.

Both call the same ``StockService``, so the thresholds are applied once, in one
place. They hold different areas: the check is part of building a quote (admin
and seller), the report is what gets bought (admin only).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.inventory.schemas import (
    LowStockReport,
    StockCheckRequest,
    StockCheckResult,
)
from src.modules.inventory.service import StockService, stock_service
from src.modules.products.model import ProductType
from src.modules.users.dependencies import require_permission
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    responses=ERROR_RESPONSES,
)

_BRANCH_QUERY = Query(
    default=None,
    alias="branchId",
    description="Restricts the report to a branch (empty = every branch with a warehouse)",
)

_PRODUCT_TYPE_QUERY = Query(
    default=None,
    alias="type",
    description="Restricts the low-stock report to one product type (empty = all)",
)


@router.post(
    "/stock-check",
    response_model=DataResponse[StockCheckResult],
    dependencies=[Depends(require_permission("inventory:check"))],
)
def check_stock(data: StockCheckRequest, svc: StockService = Depends(stock_service)):
    """Low-stock alerts for the products a quote consumes, in one branch.

    A POST and not a GET because the question carries the quantities the quote
    needs, which is half of the answer: a product can be over its threshold and
    still not cover this particular cut list.

    Never fails on the vendor's account: an unconfigured warehouse or an
    unreachable SIFAC comes back as ``checked: false`` with no alerts, because
    this is information beside a quote and must not be able to block one.
    """
    return ok(svc.check(data.branch_id, data.items))


@router.get(
    "/low-stock",
    response_model=DataResponse[LowStockReport],
    dependencies=[Depends(require_permission("inventory:low-stock"))],
)
def get_low_stock(
    branch_id: Optional[int] = _BRANCH_QUERY,
    product_type: Optional[ProductType] = _PRODUCT_TYPE_QUERY,
    svc: StockService = Depends(stock_service),
):
    """Boards and edge bandings below their configured threshold, per branch.

    It takes no date range, and deliberately: stock is a state right now, not a
    metric over a window — asking for "low stock last March" has no answer the
    vendor's system could give. That is also why it lives here and not under
    ``/analytics``: it is what the admin reorders from, not a look back.

    Unpaginated. The worst case is bounded by the catalog (a few hundred rows
    with everything at zero), and the point of the screen is to be read top to
    bottom in buying order: branch, then type, then emptiest first.
    """
    return ok(
        svc.low_stock(
            branch_id=branch_id,
            product_type=product_type.value if product_type else None,
        )
    )
