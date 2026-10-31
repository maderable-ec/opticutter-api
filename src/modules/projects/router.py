from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.modules.projects.model import ProjectStatus
from src.modules.projects.schemas import (
    ProjectCreate,
    ProjectResponse,
    ProjectStatusUpdate,
    ProjectSummaryResponse,
    ProjectUpdate,
)
from src.modules.projects.service import ProjectService, owner_scope, project_service
from src.modules.users.dependencies import get_current_user, require_permission
from src.modules.users.model import UserModel
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

# The designers' projects. Read: admin, vendedor and proyectista; write: admin
# and proyectista. ``owner_scope`` narrows the designer to their own projects.
router = APIRouter(prefix="/projects", tags=["projects"], responses=ERROR_RESPONSES)

_READ = Depends(require_permission("projects:read"))
_WRITE = Depends(require_permission("projects:write"))


def _scope(current_user: UserModel = Depends(get_current_user)) -> Optional[int]:
    return owner_scope(current_user)


@router.post(
    "/",
    response_model=DataResponse[ProjectResponse],
    status_code=201,
    dependencies=[_WRITE],
)
def create_project(
    data: ProjectCreate,
    svc: ProjectService = Depends(project_service),
    current_user: UserModel = Depends(get_current_user),
):
    """Creates a project in progress, owned by the caller."""
    return ok(svc.create_owned(data, owner_id=current_user.id))


@router.get(
    "/", response_model=PaginatedResponse[ProjectSummaryResponse], dependencies=[_READ]
)
def list_projects(
    status: Optional[List[ProjectStatus]] = Query(
        default=None,
        description="Filter by one or more statuses (repeat the parameter); the "
        "sellers' queue is `status=ready`",
    ),
    owner_id: Optional[int] = Query(
        default=None,
        alias="ownerId",
        description="Admin/seller only: narrows the listing to one designer",
    ),
    search: Optional[str] = Query(
        default=None, description="Search by project name or client name"
    ),
    paging: PageParams = Depends(),
    svc: ProjectService = Depends(project_service),
    scope: Optional[int] = Depends(_scope),
):
    """Lists projects (summary, without ``payload``), most recently touched first.

    The designer only sees their own; the admin and the seller see all of them.
    """
    items, total = svc.list_projects(
        scope=scope,
        status=status,
        owner_filter=owner_id,
        search=search,
        limit=paging.limit,
        offset=paging.offset,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/{project_id}", response_model=DataResponse[ProjectResponse], dependencies=[_READ]
)
def get_project(
    project_id: int,
    svc: ProjectService = Depends(project_service),
    scope: Optional[int] = Depends(_scope),
):
    """Gets a project with its ``payload`` and the pre-orders quoted from it."""
    return ok(svc.get_owned_or_404(project_id, scope))


@router.put(
    "/{project_id}", response_model=DataResponse[ProjectResponse], dependencies=[_WRITE]
)
def update_project(
    project_id: int,
    data: ProjectUpdate,
    svc: ProjectService = Depends(project_service),
    scope: Optional[int] = Depends(_scope),
):
    """Overwrites a project; only while it is in progress (422 otherwise)."""
    return ok(svc.update_owned(project_id, data, scope=scope))


@router.patch(
    "/{project_id}/status",
    response_model=DataResponse[ProjectResponse],
    dependencies=[_WRITE],
)
def change_project_status(
    project_id: int,
    data: ProjectStatusUpdate,
    svc: ProjectService = Depends(project_service),
    scope: Optional[int] = Depends(_scope),
):
    """Hands a project to the sellers (`ready`), reopens it or archives it."""
    return ok(svc.change_status(project_id, data.status, scope=scope))
