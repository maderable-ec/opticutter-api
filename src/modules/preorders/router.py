from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query

from src.modules.optimizations.proforma import ProformaService, pdf_response
from src.modules.preorders.model import PreOrderModel, PreOrderStatus
from src.modules.preorders.review_service import (
    PreOrderReviewService,
    preorder_review_service,
)
from src.modules.preorders.schemas import (
    PreOrderCreate,
    PreOrderResponse,
    PreOrderSummaryResponse,
    PreOrderUpdate,
    ReviewLinkInfoResponse,
    ReviewLinkResponse,
)
from src.modules.preorders.service import PreOrderService, preorder_service
from src.modules.users.dependencies import (
    get_branch_scope,
    get_current_user,
    require_permission,
)
from src.modules.users.model import UserModel
from src.shared.audit import staff_actor
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

# Pre-orders (internal quote): "administrador" and "vendedor"
# (RESOURCE_ROLES["preorders"]). The public client flow lives in
# ``public_router.py`` and authenticates solely via the link token.
router = APIRouter(
    prefix="/preorders",
    tags=["preorders"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("preorders"))],
)

_FORMAT_QUERY = Query(
    default="pdf",
    description="Output format: 'pdf' (file) or 'base64' (JSON)",
    pattern="^(pdf|base64)$",
)


def _detail(svc: PreOrderService, preorder: PreOrderModel) -> PreOrderResponse:
    """Pre-order detail with its recomputed optimization (live prices)."""
    return PreOrderResponse(
        id=preorder.id,
        code=preorder.code,
        client=preorder.client,
        branch=preorder.branch,
        status=PreOrderStatus(preorder.status),
        price_tier_code=preorder.price_tier_code,
        strategy=preorder.strategy,
        notes=preorder.notes,
        client_note=preorder.client_note,
        source=preorder.source,
        order_id=preorder.order_id,
        created_at=preorder.created_at,
        updated_at=preorder.updated_at,
        sent_at=preorder.sent_at,
        confirmed_at=preorder.confirmed_at,
        expires_at=preorder.expires_at,
        materials=preorder.materials,
        requirements=preorder.requirements,
        additional_services=preorder.additional_services,
        optimization=svc.build_optimize_response(preorder),
        history=preorder.history,
    )


@router.post("/", response_model=DataResponse[PreOrderResponse], status_code=201)
def create_preorder(
    data: PreOrderCreate,
    svc: PreOrderService = Depends(preorder_service),
    current_user: UserModel = Depends(get_current_user),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Creates a pre-order (mutable quote) with the optimizer inputs.

    The operator creates it in their own branch; the seller defaults to their
    base branch (can override it with ``branchId``); the admin must provide
    ``branchId``.
    """
    return ok(
        _detail(
            svc,
            svc.create(
                data,
                actor=staff_actor(current_user),
                branch_scope=branch_scope,
                default_branch_id=current_user.branch_id,
            ),
        )
    )


@router.get("/", response_model=PaginatedResponse[PreOrderSummaryResponse])
def list_preorders(
    status: Optional[List[PreOrderStatus]] = Query(
        default=None,
        description="Filter pre-orders by one or more statuses (repeat the parameter)",
    ),
    client_id: Optional[int] = Query(
        default=None, alias="clientId", description="Filter by client"
    ),
    branch_id: Optional[int] = Query(
        default=None,
        alias="branchId",
        description="Global roles only (admin/seller): narrows the listing to a "
        "branch (empty = all)",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Search by quote code or id, or by client identifier/name",
    ),
    created_from: Optional[date] = Query(
        default=None,
        alias="createdFrom",
        description="Only pre-orders created on or after this day (UTC, inclusive)",
    ),
    created_to: Optional[date] = Query(
        default=None,
        alias="createdTo",
        description="Only pre-orders created on or before this day (UTC, inclusive)",
    ),
    sort: Literal["oldest", "recent"] = Query(
        default="recent",
        description="Listing order: 'recent' first (the default this listing has "
        "always had) or 'oldest' first",
    ),
    paging: PageParams = Depends(),
    svc: PreOrderService = Depends(preorder_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Lists pre-orders (lightweight summary) with optional filters and pagination.

    The operator only sees the ones in their branch; global roles (admin/seller)
    see all of them (or filter with ``branchId``).
    """
    items, total = svc.list_preorders(
        status=status,
        client_id=client_id,
        branch_scope=branch_scope,
        branch_filter=branch_id,
        limit=paging.limit,
        offset=paging.offset,
        search=search,
        created_from=created_from,
        created_to=created_to,
        sort=sort,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get("/{preorder_id}", response_model=DataResponse[PreOrderResponse])
def get_preorder(
    preorder_id: int,
    svc: PreOrderService = Depends(preorder_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Gets a pre-order by ID with its recomputed optimization."""
    return ok(_detail(svc, svc.get_scoped_or_404(preorder_id, branch_scope)))


@router.put("/{preorder_id}", response_model=DataResponse[PreOrderResponse])
def update_preorder(
    preorder_id: int,
    data: PreOrderUpdate,
    svc: PreOrderService = Depends(preorder_service),
    current_user: UserModel = Depends(get_current_user),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Edits an open pre-order (draft/sent)."""
    return ok(
        _detail(
            svc,
            svc.update(
                preorder_id,
                data,
                actor=staff_actor(current_user),
                branch_scope=branch_scope,
            ),
        )
    )


@router.delete("/{preorder_id}", status_code=204)
def delete_preorder(
    preorder_id: int,
    svc: PreOrderService = Depends(preorder_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Deletes a pre-order (unless already confirmed)."""
    svc.delete(preorder_id, branch_scope=branch_scope)


@router.post(
    "/{preorder_id}/review-link",
    response_model=DataResponse[ReviewLinkResponse],
    status_code=201,
)
def create_review_link(
    preorder_id: int,
    svc: PreOrderReviewService = Depends(preorder_review_service),
    current_user: UserModel = Depends(get_current_user),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Generates the client review link (revokes the previous one, if any).

    Transitions the pre-order to ``sent``. The token is only exposed in this
    response; if lost, it must be regenerated.
    """
    link, raw_token = svc.generate(
        preorder_id, actor=staff_actor(current_user), branch_scope=branch_scope
    )
    return ok(
        ReviewLinkResponse(
            token=raw_token,
            url=svc.build_url(raw_token),
            status=link.status,
            expires_at=link.expires_at,
            created_at=link.created_at,
        )
    )


@router.get(
    "/{preorder_id}/review-link", response_model=DataResponse[ReviewLinkInfoResponse]
)
def get_review_link_info(
    preorder_id: int,
    svc: PreOrderReviewService = Depends(preorder_review_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Metadata of the latest review link (without the token, unrecoverable)."""
    return ok(svc.get_latest_info(preorder_id, branch_scope=branch_scope))


# Exempt from the JSON envelope: PDF file transport (StreamingResponse/base64).
@router.get("/{preorder_id}/proforma")
def get_preorder_proforma(
    preorder_id: int,
    format: str = _FORMAT_QUERY,
    svc: PreOrderService = Depends(preorder_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Recomputed commercial proforma (live prices) of the pre-order.

    The cut-layout diagram is omitted: the proforma is a commercial quote, so it
    lists priced requirements and materials only (the diagram lives in the
    production sheet / order document).
    """
    preorder = svc.get_scoped_or_404(preorder_id, branch_scope)
    carrier = svc.build_carrier(preorder)
    pdf_buffer = ProformaService.generate_proforma_pdf(carrier, include_diagram=False)
    return pdf_response(
        pdf_buffer, f"proforma_{preorder.code or preorder.id}.pdf", format
    )
