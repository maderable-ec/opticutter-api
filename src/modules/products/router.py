from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.modules.products.model import ProductType
from src.modules.products.schemas import ProductCreate, ProductResponse, ProductUpdate
from src.modules.products.service import ProductService, product_service
from src.modules.products.types.edge_banding import BandType
from src.modules.users.dependencies import require_permission
from src.shared.exceptions import EntityNotFoundError
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

router = APIRouter(prefix="/products", tags=["products"], responses=ERROR_RESPONSES)

# Catalog read: admin + vendedor. Write: admin only.
_READ = Depends(require_permission("products:read"))
_WRITE = Depends(require_permission("products:write"))


@router.post(
    "/",
    response_model=DataResponse[ProductResponse],
    status_code=201,
    dependencies=[_WRITE],
)
def create_product(data: ProductCreate, svc: ProductService = Depends(product_service)):
    """Creates a product (``attributes`` are validated according to ``type``)."""
    return ok(svc.create(data))


@router.get(
    "/", response_model=PaginatedResponse[ProductResponse], dependencies=[_READ]
)
def list_products(
    paging: PageParams = Depends(),
    type: Optional[ProductType] = Query(None, description="Filter by product type"),
    search: Optional[str] = Query(None, description="Search by name or code"),
    is_active: Optional[bool] = Query(
        None, description="Filter by active flag; omit to list both"
    ),
    svc: ProductService = Depends(product_service),
):
    """Lists products with optional type/active filters, search, and pagination.

    Results are ordered by name, so paging through them is stable.
    """
    items, total = svc.search_paginated(
        search, type, paging.limit, paging.offset, is_active
    )
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/{board_id}/edge-bandings",
    response_model=DataResponse[List[ProductResponse]],
    dependencies=[_READ],
)
def get_board_edge_bandings(
    board_id: int,
    band_type: Optional[BandType] = Query(
        None, description="Filter by band type: Soft or Hard"
    ),
    svc: ProductService = Depends(product_service),
):
    """Edge bandings coordinated with a board (same family and width-by-thickness).

    Matches on the explicit ``family`` attribute shared by the board and its
    edge bandings and applies the thickness→width rule, over active products
    only. An empty ``data`` means there's no coordinated edge banding for that
    combination.
    """
    return ok(svc.find_edge_bandings_for_board(board_id, band_type))


@router.get(
    "/{product_id}", response_model=DataResponse[ProductResponse], dependencies=[_READ]
)
def get_product(product_id: int, svc: ProductService = Depends(product_service)):
    """Gets a product by ID."""
    return ok(svc.get_or_404(product_id))


@router.get(
    "/code/{code}", response_model=DataResponse[ProductResponse], dependencies=[_READ]
)
def get_product_by_code(code: str, svc: ProductService = Depends(product_service)):
    """Gets a product by code."""
    product = svc.get_by_code(code)
    if product is None:
        raise EntityNotFoundError("Product", code)
    return ok(product)


@router.put(
    "/{product_id}", response_model=DataResponse[ProductResponse], dependencies=[_WRITE]
)
def update_product(
    product_id: int,
    data: ProductUpdate,
    svc: ProductService = Depends(product_service),
):
    """Updates a product."""
    return ok(svc.update(product_id, data))


@router.delete("/{product_id}", status_code=204, dependencies=[_WRITE])
def delete_product(product_id: int, svc: ProductService = Depends(product_service)):
    """Deletes a product."""
    svc.delete(product_id)
