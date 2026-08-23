from typing import Annotated, List, Literal, Optional, Union

from pydantic import Field, confloat

from src.modules.products.model import ProductType
from src.modules.products.types.board import BoardAttributes
from src.modules.products.types.edge_banding import EdgeBandingAttributes
from src.shared.schemas import CamelModel


class ProductBase(CamelModel):
    """Fields common to all catalog products."""

    code: str = Field(..., min_length=1, max_length=32, description="Unique code")
    name: str = Field(..., min_length=1, max_length=128, description="Unique name")
    description: Optional[str] = Field(None, max_length=256, description="Description")
    price: confloat(ge=0) = Field(..., description="Sale price")
    is_active: bool = Field(True, description="Whether the product is active")


class BoardProductCreate(ProductBase):
    type: Literal[ProductType.BOARD]
    attributes: BoardAttributes


class EdgeBandingProductCreate(ProductBase):
    type: Literal[ProductType.EDGE_BANDING]
    attributes: EdgeBandingAttributes


# Union discriminated by ``type``: FastAPI/Pydantic v2 pick and validate the
# correct ``attributes`` schema based on the type sent. A new type = one more branch.
ProductCreate = Annotated[
    Union[BoardProductCreate, EdgeBandingProductCreate],
    Field(discriminator="type"),
]


class ProductUpdate(CamelModel):
    """Partial update; ``attributes`` is validated against the existing type's
    schema in the service (a product's type never changes after creation)."""

    code: Optional[str] = Field(None, min_length=1, max_length=32)
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=256)
    price: Optional[confloat(ge=0)] = None
    is_active: Optional[bool] = None
    attributes: Optional[dict] = None


class ProductResponse(CamelModel):
    """Catalog response: common fields + the type's attributes bag."""

    id: int
    type: ProductType
    code: str
    external_code: Optional[str] = Field(
        None,
        description="Set by the external catalog sync; null for hand-created products",
    )
    name: str
    description: Optional[str] = None
    price: float
    is_active: bool
    attributes: dict


class ProductSyncIssue(CamelModel):
    """One source row the sync could not import.

    Carries the vendor's own code and article name, because fixing it means
    finding that row in the inventory system — a row number would be useless.
    """

    code: str
    name: str
    message: str


class ProductSyncResult(CamelModel):
    """Summary of a sync against the external inventory system."""

    created: int
    updated: int
    deactivated: int
    deleted: int
    skipped_medio: int
    # Rows the source returned but has taken out of service (est/FecEli). They
    # are reported rather than silently dropped: together with `deleted` and
    # `deactivated` they explain why the catalog shrank.
    skipped_inactive: int = 0
    # Rows whose data the sync could not parse. They are skipped, never fatal:
    # the source is a live database, so an article with no usable dimensions
    # can't be "fixed and re-uploaded" before every run. See `issues`.
    skipped_invalid: int = 0
    issues: List[ProductSyncIssue] = []
    # True when nothing was written — the pass ran and rolled back.
    dry_run: bool = False
