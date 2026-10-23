from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.modules.users.dependencies import get_current_user, require_permission
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.modules.users.refresh_token_service import (
    RefreshTokenService,
    refresh_token_service,
)
from src.modules.users.schemas import UserCreate, UserResponse, UserUpdate
from src.modules.users.service import UserService, user_service
from src.shared.crud import ListSort
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

# User management: "administrador" only (RESOURCE_ROLES["users:manage"]).
router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("users:manage"))],
)


@router.post("/", response_model=DataResponse[UserResponse], status_code=201)
def create_user(data: UserCreate, svc: UserService = Depends(user_service)):
    """Creates a new user."""
    return ok(svc.create(data))


@router.get("/", response_model=PaginatedResponse[UserResponse])
def list_users(
    paging: PageParams = Depends(),
    search: Optional[str] = Query(None, description="Search by email or name"),
    role: Optional[List[UserRole]] = Query(
        default=None,
        description="Filter by one or more roles (repeat the parameter)",
    ),
    branch_id: Optional[int] = Query(
        default=None, alias="branchId", description="Filter by assigned branch"
    ),
    is_active: Optional[bool] = Query(
        None, alias="isActive", description="Filter by active flag; omit to list both"
    ),
    sort: ListSort = Query(
        default="name",
        description="Listing order: by name (default), or newest/oldest first",
    ),
    svc: UserService = Depends(user_service),
):
    """Lists users with optional search, filters, ordering and pagination."""
    items, total = svc.list_users(
        search=search,
        roles=role,
        branch_id=branch_id,
        is_active=is_active,
        sort=sort,
        limit=paging.limit,
        offset=paging.offset,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get("/{user_id}", response_model=DataResponse[UserResponse])
def get_user(user_id: int, svc: UserService = Depends(user_service)):
    """Gets a user by ID."""
    return ok(svc.get_or_404(user_id))


@router.put("/{user_id}", response_model=DataResponse[UserResponse])
def update_user(
    user_id: int,
    data: UserUpdate,
    current_user: UserModel = Depends(get_current_user),
    svc: UserService = Depends(user_service),
    refresh_svc: RefreshTokenService = Depends(refresh_token_service),
):
    """Updates a user (includes password change and logical deactivation).

    A user is never deleted: their name is the author of cut pieces, activities,
    payments and history rows, and a delete nulls all of it out. ``isActive:
    false`` is how one leaves. It also revokes their refresh tokens, so
    reactivating them later doesn't bring an old session back.
    """
    user = svc.update(user_id, data, actor_id=current_user.id)
    if not user.is_active:
        refresh_svc.revoke_all_for_user(user.id)
    return ok(user)
