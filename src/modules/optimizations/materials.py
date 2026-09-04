"""Source-agnostic material resolution for the cutting engine.

The optimizer only needs dimensions and cost. This resolver translates each
``MaterialInput`` (catalog / offcut / manual) into a uniform ``ResolvedMaterial``,
isolating the coupling with the product catalog to a single point. Supporting a
new source only requires adding its ``source`` and its branch (in ``schemas.py``
and here); the ``cutting`` domain stays unchanged.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from src.modules.optimizations.schemas import (
    CatalogMaterialInput,
    InlineMaterialInput,
    MaterialInput,
    MaterialSource,
    PoolFillOrder,
)
from src.modules.products.model import ProductType
from src.modules.products.service import ProductService
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError


@dataclass
class ResolvedMaterial:
    """Material ready for the optimizer: geometry + cost + origin metadata.

    ``product_id``/``code``/``name`` are only populated for catalog materials
    (used for order billing and the proforma); inline sources leave them as
    ``None`` (``name`` may carry the material's free-text label).
    """

    key: str
    width: float
    height: float
    thickness: float
    # Always the LIST price (level 1) for a catalog board, whatever level the
    # seller is quoting: this is what the search optimizes against and what
    # ``_compute_hash`` records, so one cut plan is cached per job instead of one
    # per level. The chosen level is applied to the finished payload by
    # ``price_levels.apply_price_level``.
    cost_per_unit: float
    source: str
    product_id: Optional[int] = None
    code: Optional[str] = None
    name: Optional[str] = None
    # Pool metadata: ``quantity`` is the finite supply of an offcut (``None``
    # for the unlimited sources), ``pool_key`` names the anchor this material is
    # extra stock of, and ``fill_order`` applies to a catalog board anchoring a
    # pool.
    quantity: Optional[int] = None
    pool_key: Optional[str] = None
    fill_order: PoolFillOrder = PoolFillOrder.auto
    # The catalog's reduced price levels, ``None`` when the vendor never loaded
    # them. Deliberately NOT serialized by ``to_dict``: baking them into the
    # cached payload would let a catalog price edit stay invisible for a whole
    # OPT_RESULT_TTL_SECONDS, and they are re-read from the DB on every request
    # anyway (the resolution runs before the cache lookup, for the hash).
    price_2: Optional[float] = None
    price_3: Optional[float] = None

    @property
    def is_catalog(self) -> bool:
        return self.source == MaterialSource.catalog.value

    @property
    def is_finite(self) -> bool:
        """Whether this material is a countable physical sheet.

        The single definition of "there are only so many of these": an offcut is
        a piece somebody already owns, while a catalog board is bought on demand
        and a ``manual`` measurement is a board *type*, not a unique piece. It
        decides both the supply handed to the search and whether a sheet counts
        as a board on the document.
        """
        return self.source in (
            MaterialSource.company_offcut.value,
            MaterialSource.client_offcut.value,
        )

    def to_dict(self) -> dict:
        """Serializable form for the optimization snapshot/payload."""
        return {
            "material_key": self.key,
            "source": self.source,
            "product_id": self.product_id,
            "product_code": self.code,
            "product_name": self.name,
            "width": self.width,
            "height": self.height,
            "thickness": self.thickness,
            "cost_per_unit": self.cost_per_unit,
        }


class MaterialResolver:
    """Resolves each ``MaterialInput`` to a ``ResolvedMaterial`` based on its ``source``."""

    def __init__(self, db: Session):
        self.product_service = ProductService(db)

    def resolve(self, material: MaterialInput) -> ResolvedMaterial:
        if isinstance(material, CatalogMaterialInput):
            return self._resolve_catalog(material)
        return self._resolve_inline(material)

    def _resolve_catalog(self, material: CatalogMaterialInput) -> ResolvedMaterial:
        """Resolves a catalog board: 404 if it doesn't exist, 422 if it isn't a board."""
        product = self.product_service.get(material.product_id)
        if product is None:
            raise EntityNotFoundError("Product", material.product_id)
        if product.type != ProductType.BOARD.value:
            raise BusinessRuleError(
                f"El producto {product.code} no es un tablero optimizable"
            )
        attrs = product.attributes
        return ResolvedMaterial(
            key=material.key,
            width=attrs["width"],
            height=attrs["height"],
            thickness=attrs["thickness"],
            cost_per_unit=product.price,
            source=MaterialSource.catalog.value,
            product_id=product.id,
            code=product.code,
            name=product.name,
            fill_order=material.fill_order,
            price_2=product.price_2,
            price_3=product.price_3,
        )

    def _resolve_inline(self, material: InlineMaterialInput) -> ResolvedMaterial:
        """Company/client offcut or manual measurement: dimensions and cost from the input.

        ``quantity`` becomes real supply for the offcut sources (defaulting to
        one physical sheet) and stays ``None`` — unlimited — for ``manual``.
        """
        is_offcut = material.source in (
            MaterialSource.company_offcut,
            MaterialSource.client_offcut,
        )
        return ResolvedMaterial(
            key=material.key,
            width=material.width,
            height=material.height,
            thickness=material.thickness,
            cost_per_unit=material.cost_per_unit,
            source=material.source.value,
            product_id=None,
            code=None,
            name=material.label,
            quantity=(material.quantity or 1) if is_offcut else None,
            pool_key=material.pool_key,
        )
