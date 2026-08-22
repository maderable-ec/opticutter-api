from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.additional_services.schemas import (
    AdditionalServiceCreate,
    AdditionalServiceResponse,
    AdditionalServiceUpdate,
)
from src.modules.additional_services.service import (
    AdditionalServiceService,
    additional_service_service,
)
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

router = APIRouter(
    prefix="/additional-services",
    tags=["additional-services"],
    responses=ERROR_RESPONSES,
)

# Catalog read: admin + vendedor (the seller needs it for the quote picker).
# Write (create/update/delete): admin only.
_READ = Depends(require_permission("additional_services:read"))
_WRITE = Depends(require_permission("additional_services:write"))


@router.post(
    "/",
    response_model=DataResponse[AdditionalServiceResponse],
    status_code=201,
    dependencies=[_WRITE],
)
def create_additional_service(
    data: AdditionalServiceCreate,
    svc: AdditionalServiceService = Depends(additional_service_service),
):
    """Creates an additional service."""
    return ok(svc.create(data))


@router.get(
    "/",
    response_model=PaginatedResponse[AdditionalServiceResponse],
    dependencies=[_READ],
)
def list_additional_services(
    paging: PageParams = Depends(),
    search: Optional[str] = Query(None, description="Search by name"),
    is_active: Optional[bool] = Query(
        None, alias="isActive", description="Filter by active flag"
    ),
    sort: ListSort = Query(
        default="name",
        description="Listing order: by name (default), or newest/oldest first",
    ),
    svc: AdditionalServiceService = Depends(additional_service_service),
):
    """Lists additional services with optional search/active filter and pagination."""
    items, total = svc.search_paginated(
        search, is_active, paging.limit, paging.offset, sort=sort
    )
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/{service_id}",
    response_model=DataResponse[AdditionalServiceResponse],
    dependencies=[_READ],
)
def get_additional_service(
    service_id: int,
    svc: AdditionalServiceService = Depends(additional_service_service),
):
    """Gets an additional service by ID."""
    return ok(svc.get_or_404(service_id))


@router.put(
    "/{service_id}",
    response_model=DataResponse[AdditionalServiceResponse],
    dependencies=[_WRITE],
)
def update_additional_service(
    service_id: int,
    data: AdditionalServiceUpdate,
    svc: AdditionalServiceService = Depends(additional_service_service),
):
    """Updates an additional service."""
    return ok(svc.update(service_id, data))


@router.delete("/{service_id}", status_code=204, dependencies=[_WRITE])
def delete_additional_service(
    service_id: int,
    svc: AdditionalServiceService = Depends(additional_service_service),
):
    """Deletes an additional service."""
    svc.delete(service_id)
