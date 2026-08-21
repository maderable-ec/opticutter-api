from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.products.model import ProductModel, ProductType
from src.modules.products.registry import attributes_schema_for
from src.modules.products.schemas import ProductBase, ProductCreate, ProductUpdate
from src.modules.products.types.edge_banding import BandType
from src.shared.crud import CRUDService
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError

# Business rule: edge banding width (mm) coordinated with the board thickness.
# The banding must cover the board's edge plus a margin, so a 15 mm board uses
# 19 mm tape and a 36 mm board uses 40 mm tape.
BOARD_THICKNESS_TO_EDGE_WIDTH = {15: 19, 36: 40}


class ProductService(CRUDService[ProductModel, ProductBase, ProductUpdate]):
    """Product catalog CRUD + searches and per-type attribute validation.

    ``create``/``update`` are overridden because the payload carries an
    ``attributes`` submodel discriminated by ``type`` that gets persisted as
    JSON (in the API's canonical camelCase shape).
    """

    model = ProductModel
    conflict_messages = {
        "code": "El código del producto ya existe",
        "name": "El nombre del producto ya existe",
    }

    def create(self, data: ProductCreate) -> ProductModel:
        payload = data.model_dump()
        payload["type"] = data.type.value
        # mode="json" guarantees JSON-serializable values (enums -> their value)
        # for the ``attributes`` bag persisted in the JSON column.
        payload["attributes"] = data.attributes.model_dump(by_alias=True, mode="json")
        return self._persist(ProductModel(**payload))

    def update(self, id: int, data: ProductUpdate) -> ProductModel:
        obj = self.get_or_404(id)
        fields = data.model_dump(exclude_unset=True)
        if fields.get("attributes") is not None:
            schema = attributes_schema_for(obj.type)
            fields["attributes"] = schema(**fields["attributes"]).model_dump(
                by_alias=True, mode="json"
            )
        for field, value in fields.items():
            setattr(obj, field, value)
        return self._persist(obj)

    def get_by_code(self, code: str) -> Optional[ProductModel]:
        """Gets a product by its code."""
        return self.db.query(ProductModel).filter(ProductModel.code == code).first()

    def search_paginated(
        self,
        search: Optional[str] = None,
        type: Optional[ProductType] = None,
        limit: int = 20,
        offset: int = 0,
        is_active: Optional[bool] = None,
        subtype: Optional[str] = None,
    ) -> Tuple[List[ProductModel], int]:
        """Lists products filtering by type, active flag, subtype and/or text.

        Ordered by ``name`` (unique, so the order is total) to make paging
        stable: without it Postgres may repeat or skip rows across pages.
        """
        query = self.db.query(ProductModel)
        if type is not None:
            query = query.filter(ProductModel.type == ProductType(type).value)
        if is_active is not None:
            query = query.filter(ProductModel.is_active.is_(is_active))
        if subtype:
            query = query.filter(
                ProductModel.attributes["subtype"].as_string().ilike(subtype)
            )
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                ProductModel.code.ilike(pattern) | ProductModel.name.ilike(pattern)
            )
        query = query.order_by(ProductModel.name, ProductModel.id)
        return self._paginate(query, limit, offset)

    @staticmethod
    def _norm_family(value: Optional[str]) -> str:
        """Normalizes a family value for matching (trim + case-insensitive)."""
        return (value or "").strip().casefold()

    def find_edge_bandings_for_board(
        self, board_id: int, band_type: Optional[BandType] = None
    ) -> List[ProductModel]:
        """Edge bandings coordinated with a board (same family and correct width).

        Matches on the explicit ``family`` attribute shared by the board and its
        edge bandings (user-configurable, unlike the editable ``code``) and
        applies the thickness→width rule (``BOARD_THICKNESS_TO_EDGE_WIDTH``).
        Optionally filters by band type (``BandType``). Inactive products are
        never coordinated. Returns ``[]`` when the board has no family, no width
        rule applies, or there's no match for that combination (a real catalog
        gap, e.g. soft banding for a 36 mm board).
        """
        board = self.get_or_404(board_id)
        if board.type != ProductType.BOARD.value:
            raise BusinessRuleError(f"El producto {board.code} no es un tablero")

        board_family = self._norm_family(board.attributes.get("family"))
        if not board_family:
            return []

        target_width = BOARD_THICKNESS_TO_EDGE_WIDTH.get(
            int(board.attributes["thickness"])
        )
        if target_width is None:
            return []

        candidates = (
            self.db.query(ProductModel)
            .filter(
                ProductModel.type == ProductType.EDGE_BANDING.value,
                ProductModel.is_active.is_(True),
            )
            .all()
        )

        matches = [
            p
            for p in candidates
            if self._norm_family(p.attributes.get("family")) == board_family
            and p.attributes.get("width") == target_width
            and (band_type is None or p.attributes.get("bandType") == band_type.value)
        ]
        return sorted(matches, key=lambda p: p.attributes.get("thickness", 0))


def product_service(db: Session = Depends(get_db)) -> ProductService:
    """``ProductService`` provider for route injection."""
    return ProductService(db)
