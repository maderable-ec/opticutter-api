from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.modules.clients.client_sync import sync_clients_from_external
from src.modules.clients.schemas import (
    ClientCreate,
    ClientResponse,
    ClientSyncResult,
    ClientUpdate,
)
from src.modules.clients.service import ClientService, client_service
from src.modules.users.dependencies import require_permission
from src.shared.crud import ListSort
from src.shared.database import get_db
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


@router.post("/sync", response_model=DataResponse[ClientSyncResult])
def sync_clients(
    dry_run: bool = Query(
        False,
        alias="dryRun",
        description="Report what would change without writing anything",
    ),
    db: Session = Depends(get_db),
):
    """Upserts clients from the external system (SIFAC), matched by cédula/RUC.

    Takes no body: the source is the vendor's database (EXTERNAL_CATALOG_URL).

    Only creates and updates — a client the external system no longer brings is
    never deleted or deactivated, since orders and pre-orders point at them. A
    row it can't use (no cédula, a duplicate, a cédula that fails the check
    digit) is skipped and reported rather than aborting the run.

    On update the source only overwrites a field it has a value for, so a phone
    or e-mail typed into the dashboard survives a re-sync that brings a blank.

    Run it with ``?dryRun=true`` first: same pass, same numbers, rolled back.
    """
    return ok(sync_clients_from_external(db, dry_run=dry_run))


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
