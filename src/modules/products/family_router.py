"""Endpoints for the design families that coordinate boards with edge bandings.

A sibling prefix (``/product-families``) rather than ``/products/families``, and
that is not cosmetic: FastAPI resolves by declaration order, so a literal
one-segment path under ``/products`` would be swallowed by the existing
``GET /products/{product_id}`` and answered with "not a valid integer". A prefix
of its own has zero ordering coupling with that router.

Second router inside a module is an established shape here: ``users/auth_router``
and ``preorders/public_router`` do the same.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.products.family_schemas import (
    FamilyAliasRequest,
    FamilyAliasResult,
    FamilyAssignRequest,
    FamilyAssignResult,
    ProductFamilyCreate,
    ProductFamilyDetailResponse,
    ProductFamilyResponse,
    ProductFamilyUpdate,
)
from src.modules.products.family_service import (
    FamilyStats,
    ProductFamilyService,
    product_family_service,
)
from src.modules.products.model import ProductFamilyModel
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
    prefix="/product-families", tags=["product-families"], responses=ERROR_RESPONSES
)

# Same matrix as the catalog itself, no new permission key: a family IS catalog
# data, so reading it is ``products:read`` (admin + vendedor) and editing it is
# ``products:write`` (admin only). Deliberately the conservative default —
# moving a board between families changes what gets quoted. Opening it to the
# seller is one line in ``permissions.py`` if the business asks.
_READ = Depends(require_permission("products:read"))
_WRITE = Depends(require_permission("products:write"))


def _to_response(family: ProductFamilyModel, stats: FamilyStats) -> dict:
    """Merges the row with its derived stats into the response shape."""
    return {
        "id": family.id,
        "name": family.name,
        "description": family.description,
        **stats.as_dict(),
    }


@router.post(
    "/",
    response_model=DataResponse[ProductFamilyResponse],
    status_code=201,
    dependencies=[_WRITE],
)
def create_family(
    data: ProductFamilyCreate,
    svc: ProductFamilyService = Depends(product_family_service),
):
    """Creates a design family. A name that normalizes to an existing one is a 409."""
    family = svc.create(data)
    return ok(_to_response(family, FamilyStats()))


@router.post(
    "/assignments",
    response_model=DataResponse[FamilyAssignResult],
    dependencies=[_WRITE],
)
def assign_products(
    data: FamilyAssignRequest,
    svc: ProductFamilyService = Depends(product_family_service),
):
    """Assigns products to a family in bulk; ``familyId: null`` unassigns them.

    A POST with no path parameter, so it cannot collide with ``/{family_id}``
    whatever the declaration order — the trap that a ``PUT /assignments`` would
    have walked straight into.

    All-or-nothing: an unknown product id is a 404 and nothing is written.
    """
    assigned = svc.assign(data.family_id, data.product_ids)
    return ok(FamilyAssignResult(assigned=assigned, family_id=data.family_id))


@router.post(
    "/{family_id}/alias",
    response_model=DataResponse[FamilyAliasResult],
    dependencies=[_WRITE],
)
def set_family_alias(
    family_id: int,
    data: FamilyAliasRequest,
    svc: ProductFamilyService = Depends(product_family_service),
):
    """Stamps one printed code onto several of the family's tapes.

    Lives here rather than only on ``PUT /products/{id}`` because this screen is
    where a missing alias is DISCOVERED — the listing counts them — and telling
    someone about a problem without offering the fix is how a diagnostic gets
    ignored. A sync that brings four new widths of one design is four product
    forms otherwise.

    All-or-nothing, and scoped: a product outside this family, or one that isn't
    an edge banding, is refused rather than obeyed.
    """
    updated = svc.set_alias(family_id, data.alias.strip(), data.product_ids)
    return ok(FamilyAliasResult(updated=updated, alias=data.alias.strip()))


@router.get(
    "/", response_model=PaginatedResponse[ProductFamilyResponse], dependencies=[_READ]
)
def list_families(
    paging: PageParams = Depends(),
    search: Optional[str] = Query(None, description="Search by name"),
    sort: ListSort = Query("name", description="name | recent | oldest"),
    issues_only: bool = Query(
        False,
        alias="issuesOnly",
        description="Only families with something to fix: no counterpart, a "
        "thickness no stocked width covers, a divergent or missing alias",
    ),
    svc: ProductFamilyService = Depends(product_family_service),
):
    """Lists families with their member counts and coverage signals.

    The signals are what used to be catalog-sync warnings ("this family has no
    counterpart", "no stocked width covers this board"). They live here because
    coordination stopped living in the vendor's ``obs``: computed from source
    rows they would flag families already fixed by hand and stay quiet about the
    ones broken from this side.
    """
    rows, total = svc.list_with_stats(
        search, sort, paging.limit, paging.offset, issues_only
    )
    items = [_to_response(family, stats) for family, stats in rows]
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/{family_id}",
    response_model=DataResponse[ProductFamilyDetailResponse],
    dependencies=[_READ],
)
def get_family(
    family_id: int, svc: ProductFamilyService = Depends(product_family_service)
):
    """One family with its stats and its members, split into boards and tapes."""
    family, stats, boards, bandings = svc.detail(family_id)
    payload = _to_response(family, stats)
    payload["boards"] = boards
    payload["edge_bandings"] = bandings
    return ok(payload)


@router.put(
    "/{family_id}",
    response_model=DataResponse[ProductFamilyResponse],
    dependencies=[_WRITE],
)
def update_family(
    family_id: int,
    data: ProductFamilyUpdate,
    svc: ProductFamilyService = Depends(product_family_service),
):
    """Renames a family or edits its notes; the matching key is recomputed."""
    family = svc.update(family_id, data)
    _, stats, _, _ = svc.detail(family.id)
    return ok(_to_response(family, stats))


@router.delete("/{family_id}", status_code=204, dependencies=[_WRITE])
def delete_family(
    family_id: int, svc: ProductFamilyService = Depends(product_family_service)
):
    """Deletes a family. Its products survive, unassigned (FK ON DELETE SET NULL).

    Non-destructive on purpose: a family is a grouping, and deleting a grouping
    must never be able to delete catalog rows.
    """
    svc.delete(family_id)
    return None
