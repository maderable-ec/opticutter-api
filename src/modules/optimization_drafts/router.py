from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.optimization_drafts.schemas import (
    DraftCreate,
    DraftResponse,
    DraftSummaryResponse,
    DraftUpdate,
)
from src.modules.optimization_drafts.service import (
    OptimizationDraftService,
    optimization_draft_service,
)
from src.modules.users.dependencies import (
    get_branch_scope,
    get_current_user,
    require_permission,
)
from src.modules.users.model import UserModel
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

# Optimizer drafts: "administrador" and "vendedor" (RESOURCE_ROLES["optimizer:drafts"]).
router = APIRouter(
    prefix="/optimization-drafts",
    tags=["optimization-drafts"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("optimizer:drafts"))],
)


@router.post("/", response_model=DataResponse[DraftResponse], status_code=201)
def create_draft(
    data: DraftCreate,
    svc: OptimizationDraftService = Depends(optimization_draft_service),
    current_user: UserModel = Depends(get_current_user),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Creates an optimizer draft.

    The operator creates it in their own branch; the seller defaults to their
    base branch (can override it with ``branchId``); the admin must provide
    ``branchId``.
    """
    return ok(
        svc.create_scoped(
            data,
            branch_scope=branch_scope,
            default_branch_id=current_user.branch_id,
        )
    )


@router.get("/", response_model=PaginatedResponse[DraftSummaryResponse])
def list_drafts(
    branch_id: Optional[int] = Query(
        default=None,
        alias="branchId",
        description="Global roles only (admin/seller): narrows the listing to a "
        "branch (empty = all)",
    ),
    paging: PageParams = Depends(),
    svc: OptimizationDraftService = Depends(optimization_draft_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Lists drafts (lightweight summary, without ``payload``) with pagination.

    The operator only sees the ones in their branch; global roles (admin/seller)
    see all of them (or filter with ``branchId``).
    """
    items, total = svc.list_scoped(
        branch_scope=branch_scope,
        branch_filter=branch_id,
        limit=paging.limit,
        offset=paging.offset,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get("/{draft_id}", response_model=DataResponse[DraftResponse])
def get_draft(
    draft_id: int,
    svc: OptimizationDraftService = Depends(optimization_draft_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Gets a draft by ID (includes the full ``payload``)."""
    return ok(svc.get_scoped_or_404(draft_id, branch_scope))


@router.put("/{draft_id}", response_model=DataResponse[DraftResponse])
def update_draft(
    draft_id: int,
    data: DraftUpdate,
    svc: OptimizationDraftService = Depends(optimization_draft_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Updates (overwrites) a draft."""
    return ok(svc.update_scoped(draft_id, data, branch_scope=branch_scope))


@router.delete("/{draft_id}", status_code=204)
def delete_draft(
    draft_id: int,
    svc: OptimizationDraftService = Depends(optimization_draft_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Deletes a draft."""
    svc.delete_scoped(draft_id, branch_scope=branch_scope)
