from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.branches.schemas import BranchCreate, BranchResponse, BranchUpdate
from src.modules.branches.service import BranchService, branch_service
from src.modules.users.dependencies import require_permission
from src.shared.crud import ListSort
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

router = APIRouter(prefix="/branches", tags=["branches"], responses=ERROR_RESPONSES)

# Administration (CRUD): "administrador" only. Read/list: any staff member
# (for selectors and to show the name of their own branch).
_READ = Depends(require_permission("branches:read"))
_WRITE = Depends(require_permission("branches:manage"))


@router.post(
    "/",
    response_model=DataResponse[BranchResponse],
    status_code=201,
    dependencies=[_WRITE],
)
def create_branch(data: BranchCreate, svc: BranchService = Depends(branch_service)):
    """Creates a branch."""
    return ok(svc.create(data))


@router.get("/", response_model=PaginatedResponse[BranchResponse], dependencies=[_READ])
def list_branches(
    paging: PageParams = Depends(),
    search: Optional[str] = Query(None, description="Search by code or name"),
    is_active: Optional[bool] = Query(
        None, alias="isActive", description="Filter by active flag; omit to list both"
    ),
    sort: ListSort = Query(
        default="name",
        description="Listing order: by name (default), or newest/oldest first",
    ),
    svc: BranchService = Depends(branch_service),
):
    """Lists branches with optional search/active filter, ordering and pagination."""
    items, total = svc.list_branches(
        search=search,
        is_active=is_active,
        sort=sort,
        limit=paging.limit,
        offset=paging.offset,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/{branch_id}", response_model=DataResponse[BranchResponse], dependencies=[_READ]
)
def get_branch(branch_id: int, svc: BranchService = Depends(branch_service)):
    """Gets a branch by ID."""
    return ok(svc.get_or_404(branch_id))


@router.put(
    "/{branch_id}", response_model=DataResponse[BranchResponse], dependencies=[_WRITE]
)
def update_branch(
    branch_id: int, data: BranchUpdate, svc: BranchService = Depends(branch_service)
):
    """Updates a branch (includes logical deactivation via ``isActive``)."""
    return ok(svc.update(branch_id, data))


@router.delete("/{branch_id}", status_code=204, dependencies=[_WRITE])
def delete_branch(branch_id: int, svc: BranchService = Depends(branch_service)):
    """Deletes a branch."""
    svc.delete(branch_id)
