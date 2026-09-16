"""Stock endpoints consumed while quoting.

Only the question a seller asks lives here. The low-stock **report** is an
administrator's report and hangs off ``/analytics`` with the rest of them; both
call the same ``StockService`` so the thresholds are applied once, in one place.
"""

from fastapi import APIRouter, Depends

from src.modules.inventory.schemas import StockCheckRequest, StockCheckResult
from src.modules.inventory.service import StockService, stock_service
from src.modules.users.dependencies import require_permission
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("inventory:check"))],
)


@router.post("/stock-check", response_model=DataResponse[StockCheckResult])
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
