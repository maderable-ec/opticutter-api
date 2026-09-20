from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.products.model import (
    ProductFamilyModel,
    ProductModel,
    ProductType,
)
from src.modules.products.registry import attributes_schema_for
from src.modules.products.schemas import ProductBase, ProductCreate, ProductUpdate
from src.modules.products.types.edge_banding import BandType
from src.shared.crud import CRUDService
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError

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

    Module-level and free of the DB because two callers must agree on what
    "compatible" means: this picker, and the family coverage report
    (``ProductFamilyService.list_with_stats``), which flags a design whose
    stocked widths cover none of its own boards. If they disagreed, the report
    would fire on pairs that do coordinate — or stay quiet on ones that don't.
    (That report used to be a catalog-sync warning computed over the vendor's
    rows; it moved when coordination stopped living there.)
    """
    overhang = banding_width - board_thickness
    return EDGE_WIDTH_MIN_OVERHANG_MM <= overhang <= EDGE_WIDTH_MAX_OVERHANG_MM


def normalize_family(value: Optional[str]) -> str:
    """Normalizes a family name for matching (trim + case-insensitive).

    This is THE definition of "the same family", and it is why
    ``product_families.normalized_name`` is a stored column rather than a unique
    index on ``lower(name)``: ``casefold()`` is not Postgres' ``lower()``, and
    two definitions would diverge the day a design name leaves ASCII. The rule
    lives here; the database only enforces uniqueness of what it is handed.

    Module-level rather than a method because both the family service (writing
    the key) and the catalog sync (resolving an incoming ``obs`` to an existing
    family) have to agree on it.
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

    def _assert_family_exists(self, family_id: Optional[int]) -> None:
        """Rejects an unknown ``family_id`` with a 404 instead of a 409.

        Without this the FK violation would surface through
        ``CRUDService._conflict_detail``, which substring-matches the driver's
        message against ``conflict_messages`` and, on a miss, answers a generic
        409 "Violación de restricción de integridad" — useless to the caller and
        wrong about what happened.
        """
        if family_id is None:
            return
        if self.db.get(ProductFamilyModel, family_id) is None:
            raise EntityNotFoundError("ProductFamily", family_id)

    @staticmethod
    def _assert_alias_allowed(product_type: str, alias: Optional[str]) -> None:
        """An alias belongs to an edge banding and always did.

        It used to be a key of ``EdgeBandingAttributes``, so a board that sent
        one had it dropped by Pydantic without a word. Now that it is a
        first-class column, silence would be the wrong answer: the database's
        own CHECK would reject it as an opaque integrity error anyway, so the
        rule is stated here where the message can say what is wrong.
        """
        if alias is not None and product_type != ProductType.EDGE_BANDING.value:
            raise BusinessRuleError("Solo un tapacanto puede llevar alias")

    def create(self, data: ProductCreate) -> ProductModel:
        payload = data.model_dump()
        payload["type"] = data.type.value
        self._assert_family_exists(payload.get("family_id"))
        self._assert_alias_allowed(payload["type"], payload.get("alias"))
        # mode="json" guarantees JSON-serializable values (enums -> their value)
        # for the ``attributes`` bag persisted in the JSON column.
        payload["attributes"] = data.attributes.model_dump(by_alias=True, mode="json")
        return self._persist(ProductModel(**payload))

    def update(self, id: int, data: ProductUpdate) -> ProductModel:
        obj = self.get_or_404(id)
        fields = data.model_dump(exclude_unset=True)
        if "family_id" in fields:
            self._assert_family_exists(fields["family_id"])
        if "alias" in fields:
            # Against the STORED type: ``ProductUpdate`` is not discriminated (a
            # product's type never changes after creation, so the request has no
            # reason to carry it).
            self._assert_alias_allowed(obj.type, fields["alias"])
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
        family_id: Optional[int] = None,
        unassigned: Optional[bool] = None,
    ) -> Tuple[List[ProductModel], int]:
        """Lists products filtering by type, active flag, subtype and/or text.

        ``type`` and ``subtype`` each accept multiple values (OR within the
        field, AND across fields) for a multi-select filter.

        ``family_id`` and ``unassigned`` are two parameters rather than one
        nullable filter because a query string cannot carry a null: ``?familyId=``
        arrives as the empty string and 422s on the way to ``int``. ``unassigned``
        is what the assignment screen pages through.

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
        if family_id is not None:
            query = query.filter(ProductModel.family_id == family_id)
        if unassigned is not None:
            query = query.filter(
                ProductModel.family_id.is_(None)
                if unassigned
                else ProductModel.family_id.isnot(None)
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

        Matches on the family both sides point at — a foreign key to
        ``product_families``, managed from this system and never touched by the
        catalog sync on update — and keeps every width that fits the board's
        thickness (``edge_width_fits_board``). Optionally filters by band type
        (``BandType``). Inactive products are never coordinated. Returns ``[]``
        when the board has no family or no stocked width covers it (a real
        catalog gap — a 36 mm board whose design only comes in 19 mm tape).

        Ordered by width, then by the banding's own thickness, so the narrowest
        tape that covers the edge comes first: that's the one the shop uses, and
        the dashboard auto-selects the head of this list. The wider alternatives
        the same design is stocked in follow it instead of being hidden. ``id``
        breaks any remaining tie, because the candidate query has no ``ORDER BY``
        and equal keys would otherwise come back in whatever order the engine
        chose.

        The family match runs in SQL; the width window and the band-type filter
        stay in Python. That split is deliberate rather than half-finished:
        ``edge_width_fits_board`` is the one definition of "this tape covers this
        edge", shared with the family coverage report, and re-expressing it in
        SQL would be a second copy free to drift. The ordering keys live inside
        the JSON bag, where ``attributes['width'].as_float()`` would treat a
        missing value differently from the ``.get(..., 0)`` below and break the
        order in silence. What the SQL filter buys is the part that was actually
        expensive: this used to load EVERY active edge banding (202 rows on the
        live catalog) and compare normalized family strings in Python, on every
        call.
        """
        board = self.get_or_404(board_id)
        if board.type != ProductType.BOARD.value:
            raise BusinessRuleError(f"El producto {board.code} no es un tablero")

        # A foreign key, so there is no "family named the empty string" case
        # left to defend against: ``ProductFamilyService`` rejects a blank name.
        if board.family_id is None:
            return []

        thickness = float(board.attributes["thickness"])

        candidates = (
            self.db.query(ProductModel)
            .filter(
                ProductModel.type == ProductType.EDGE_BANDING.value,
                ProductModel.is_active.is_(True),
                ProductModel.family_id == board.family_id,
            )
            .all()
        )

        matches = [
            p
            for p in candidates
            if edge_width_fits_board(thickness, p.attributes.get("width", 0))
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
