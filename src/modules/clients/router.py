from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.clients.schemas import ClientCreate, ClientResponse, ClientUpdate
from src.modules.clients.service import ClientService, client_service
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

# Client management: "administrador" and "vendedor" (RESOURCE_ROLES["clients:manage"]).
router = APIRouter(
    prefix="/clients",
    tags=["clients"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_permission("clients:manage"))],
)


@router.post("/", response_model=DataResponse[ClientResponse], status_code=201)
def create_client(data: ClientCreate, svc: ClientService = Depends(client_service)):
    """Creates a new client."""
    return ok(svc.create(data))


@router.get("/", response_model=PaginatedResponse[ClientResponse])
def list_clients(
    paging: PageParams = Depends(),
    search: Optional[str] = Query(
        None, description="Search by identifier, first name, or last name"
    ),
    sort: ListSort = Query(
        default="name",
        description="Listing order: by name (default), or newest/oldest first",
    ),
    svc: ClientService = Depends(client_service),
):
    """Lists clients with optional search, ordering and pagination."""
    items, total = svc.list_clients(
        search=search, sort=sort, limit=paging.limit, offset=paging.offset
    )
    return page(items, total, paging.limit, paging.offset)


@router.get("/{client_id}", response_model=DataResponse[ClientResponse])
def get_client(client_id: int, svc: ClientService = Depends(client_service)):
    """Gets a client by ID."""
    return ok(svc.get_or_404(client_id))


@router.put("/{client_id}", response_model=DataResponse[ClientResponse])
def update_client(
    client_id: int, data: ClientUpdate, svc: ClientService = Depends(client_service)
):
    """Updates a client."""
    return ok(svc.update(client_id, data))


@router.delete("/{client_id}", status_code=204)
def delete_client(client_id: int, svc: ClientService = Depends(client_service)):
    """Deletes a client."""
    svc.delete(client_id)
