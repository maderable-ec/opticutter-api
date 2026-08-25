from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.products.model import ProductModel, ProductType
from src.modules.products.registry import attributes_schema_for
from src.modules.products.schemas import ProductBase, ProductCreate, ProductUpdate
from src.modules.products.types.edge_banding import BandType
from src.shared.crud import CRUDService
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError

# Business rule: an edge banding covers a board's edge only if it is WIDER than
# the board is thick — the overhang is what the trimmer shaves off afterwards.
#
# This used to be a map of one exact width per thickness ({15: 19, 36: 40}),
# which held while the catalog was seed data. The vendor stocks the same design
# in several widths — 18/19/20/22 for 15-16 mm boards, 40/45 for 36 mm — so an
# exact width left a third of the real catalog uncoordinated (a 15 mm Cashmere
# board whose family only comes in 22 mm returned an empty picker) and hid the
# alternatives on the rest. A window absorbs a width the vendor adds later
# without a table to maintain.
#
# The bounds are what makes it a rule rather than "anything wider": the minimum
# keeps a tape that exactly matches the thickness (nothing left to trim) out,
# and the maximum keeps a 40 mm tape from being offered for a 15 mm board.
EDGE_WIDTH_MIN_OVERHANG_MM = 1
EDGE_WIDTH_MAX_OVERHANG_MM = 10


def edge_width_fits_board(board_thickness: float, banding_width: float) -> bool:
    """Whether a banding of ``banding_width`` mm can cover a board's edge.

    Module-level and free of the DB for the same reason as ``normalize_family``:
    the catalog sync reports the width gaps this rule leaves behind
    (``catalog_sync._collect_warnings``) and both sides must agree on what
    "compatible" means, or the warning would fire on pairs that do coordinate.
    """
    overhang = banding_width - board_thickness
    return EDGE_WIDTH_MIN_OVERHANG_MM <= overhang <= EDGE_WIDTH_MAX_OVERHANG_MM


def normalize_family(value: Optional[str]) -> str:
    """Normalizes a family value for matching (trim + case-insensitive).

    Module-level rather than a method because the catalog sync compares the
    same key when it reports families that lost their counterpart
    (``catalog_sync._collect_warnings``): both sides must agree on what "the
    same family" means, or the warning would fire on pairs that do coordinate.
    """
    return (value or "").strip().casefold()


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
        type: Optional[List[ProductType]] = None,
        limit: int = 20,
        offset: int = 0,
        is_active: Optional[bool] = None,
        subtype: Optional[List[str]] = None,
    ) -> Tuple[List[ProductModel], int]:
        """Lists products filtering by type, active flag, subtype and/or text.

        ``type`` and ``subtype`` each accept multiple values (OR within the
        field, AND across fields) for a multi-select filter.

        Ordered by ``name`` (unique, so the order is total) to make paging
        stable: without it Postgres may repeat or skip rows across pages.
        """
        query = self.db.query(ProductModel)
        if type:
            query = query.filter(
                ProductModel.type.in_([ProductType(t).value for t in type])
            )
        if is_active is not None:
            query = query.filter(ProductModel.is_active.is_(is_active))
        if subtype:
            query = query.filter(
                func.lower(ProductModel.attributes["subtype"].as_string()).in_(
                    [s.lower() for s in subtype]
                )
            )
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                ProductModel.code.ilike(pattern) | ProductModel.name.ilike(pattern)
            )
        query = query.order_by(ProductModel.name, ProductModel.id)
        return self._paginate(query, limit, offset)

    def find_edge_bandings_for_board(
        self, board_id: int, band_type: Optional[BandType] = None
    ) -> List[ProductModel]:
        """Edge bandings coordinated with a board (same family, covering width).

        Matches on the explicit ``family`` attribute shared by the board and its
        edge bandings (user-configurable, unlike the editable ``code``) and keeps
        every width that fits the board's thickness (``edge_width_fits_board``).
        Optionally filters by band type (``BandType``). Inactive products are
        never coordinated. Returns ``[]`` when the board has no family or no
        stocked width covers it (a real catalog gap — a 36 mm board whose design
        only comes in 19 mm tape).

        Ordered by width, then by the banding's own thickness, so the narrowest
        tape that covers the edge comes first: that's the one the shop uses, and
        the dashboard auto-selects the head of this list. The wider alternatives
        the same design is stocked in follow it instead of being hidden. ``id``
        breaks any remaining tie, because the candidate query has no ``ORDER BY``
        and equal keys would otherwise come back in whatever order the engine
        chose.
        """
        board = self.get_or_404(board_id)
        if board.type != ProductType.BOARD.value:
            raise BusinessRuleError(f"El producto {board.code} no es un tablero")

        board_family = normalize_family(board.attributes.get("family"))
        if not board_family:
            return []

        thickness = float(board.attributes["thickness"])

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
            if normalize_family(p.attributes.get("family")) == board_family
            and edge_width_fits_board(thickness, p.attributes.get("width", 0))
            and (band_type is None or p.attributes.get("bandType") == band_type.value)
        ]
        return sorted(
            matches,
            key=lambda p: (
                p.attributes.get("width", 0),
                p.attributes.get("thickness", 0),
                p.id,
            ),
        )


def product_service(db: Session = Depends(get_db)) -> ProductService:
    """``ProductService`` provider for route injection."""
    return ProductService(db)
