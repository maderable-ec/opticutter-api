"""Design families: the board<->tapacanto coordination, managed from here.

Lives in the products module rather than a slice of its own because a family
exists only to group products: every read it has is a query over ``products``,
and it shares ``normalize_family`` and ``edge_width_fits_board`` with
``ProductService``. A separate module would be a vertical slice whose every
operation reaches into another one — and would make the FK a circular import.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.products.family_schemas import (
    ProductFamilyCreate,
    ProductFamilyUpdate,
)
from src.modules.products.model import (
    ProductFamilyModel,
    ProductModel,
    ProductType,
)
from src.modules.products.service import edge_width_fits_board, normalize_family
from src.shared.crud import CRUDService
from src.shared.database import get_db
from src.shared.exceptions import EntityNotFoundError, ValidationError


class FamilyStats:
    """Counts and coverage signals for one family, derived from its members.

    A plain object rather than a dataclass because it is built in one place and
    read in one place; what matters is the derivation, below.
    """

    __slots__ = (
        "board_count",
        "edge_banding_count",
        "has_no_boards",
        "has_no_edge_bandings",
        "uncovered_thicknesses",
        "aliases",
        "missing_alias_count",
    )

    def __init__(self) -> None:
        self.board_count = 0
        self.edge_banding_count = 0
        self.has_no_boards = False
        self.has_no_edge_bandings = False
        self.uncovered_thicknesses: List[float] = []
        self.aliases: List[str] = []
        self.missing_alias_count = 0

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}


def compute_family_stats(
    members_by_family: Dict[int, Sequence[ProductModel]],
) -> Dict[int, FamilyStats]:
    """Derives every family's counts and coverage signals in one pass.

    Pure (no session), so the rules are unit-testable without a database, and
    computed in Python rather than SQL on purpose: the overhang window is
    ``edge_width_fits_board``, which already has one canonical implementation.
    Re-expressing it as a SQL predicate would be a second copy that drifts in
    silence — which is precisely how the old catalog-sync warning and the picker
    could have ended up disagreeing about what "compatible" means.

    Only ACTIVE products count. An inactive one coordinates with nothing, so
    counting it would show a family as healthy while its picker comes back empty.

    Two judgement calls worth keeping:

    * ``uncovered_thicknesses`` is only computed when the family HAS tapes. With
      none, ``has_no_edge_bandings`` already says it, and saying it twice is what
      makes a diagnostic list stop being read.
    * A board with no family is not this function's problem and never appears
      here: 76 of the catalog's 210 boards (plywood, OSB, MDF fondo) coordinate
      with nothing at all, and that is N/A rather than a defect.
    """
    stats: Dict[int, FamilyStats] = {}
    for family_id, members in members_by_family.items():
        st = FamilyStats()
        thicknesses: set = set()
        widths: List[float] = []
        aliases: set = set()

        for p in members:
            if not p.is_active:
                continue
            attrs = p.attributes or {}
            if p.type == ProductType.BOARD.value:
                st.board_count += 1
                thickness = attrs.get("thickness")
                if thickness is not None:
                    thicknesses.add(float(thickness))
            elif p.type == ProductType.EDGE_BANDING.value:
                st.edge_banding_count += 1
                width = attrs.get("width")
                if width is not None:
                    widths.append(float(width))
                if p.alias:
                    aliases.add(p.alias)
                else:
                    st.missing_alias_count += 1

        st.has_no_boards = st.board_count == 0
        st.has_no_edge_bandings = st.edge_banding_count == 0
        st.aliases = sorted(aliases)
        if widths:
            st.uncovered_thicknesses = sorted(
                t
                for t in thicknesses
                if not any(edge_width_fits_board(t, w) for w in widths)
            )
        stats[family_id] = st
    return stats


class ProductFamilyService(
    CRUDService[ProductFamilyModel, ProductFamilyCreate, ProductFamilyUpdate]
):
    """CRUD over the design families plus the coverage report and bulk assignment."""

    model = ProductFamilyModel
    conflict_messages = {
        # The constraint is ``uq_product_families_normalized_name`` (naming
        # convention in ``shared/database.py``), so the substring match in
        # ``_conflict_detail`` lands on this entry.
        "normalized_name": "Ya existe una familia con ese nombre",
    }

    @staticmethod
    def _key(name: str) -> str:
        key = normalize_family(name)
        if not key:
            raise ValidationError("El nombre de la familia no puede estar vacío")
        return key

    def create(self, data: ProductFamilyCreate) -> ProductFamilyModel:
        payload = data.model_dump()
        payload["name"] = data.name.strip()
        payload["normalized_name"] = self._key(data.name)
        return self._persist(ProductFamilyModel(**payload))

    def update(self, id: int, data: ProductFamilyUpdate) -> ProductFamilyModel:
        obj = self.get_or_404(id)
        fields = data.model_dump(exclude_unset=True)
        if fields.get("name") is not None:
            fields["normalized_name"] = self._key(fields["name"])
            fields["name"] = fields["name"].strip()
        for field, value in fields.items():
            setattr(obj, field, value)
        return self._persist(obj)

    def _members_by_family(
        self, family_ids: Sequence[int]
    ) -> Dict[int, List[ProductModel]]:
        """Every member of the given families, in ONE query.

        The page's families are resolved first and their members fetched in a
        single ``IN``: the alternative (a relationship access per row) is a query
        per family, which is what makes a listing quietly quadratic.
        """
        members: Dict[int, List[ProductModel]] = {fid: [] for fid in family_ids}
        if not family_ids:
            return members
        rows = (
            self.db.query(ProductModel)
            .filter(ProductModel.family_id.in_(family_ids))
            .all()
        )
        for row in rows:
            members[row.family_id].append(row)
        return members

    def list_with_stats(
        self,
        search: Optional[str] = None,
        sort: str = "name",
        limit: int = 20,
        offset: int = 0,
        issues_only: bool = False,
    ) -> Tuple[List[Tuple[ProductFamilyModel, FamilyStats]], int]:
        """A page of families, each with its counts and coverage signals.

        ``issues_only`` filters AFTER the stats are derived, which means it
        filters the page rather than the table — the honest alternative would be
        to compute coverage for all 75 families on every request. At this size
        that is cheap, so the filter raises the page size internally instead of
        paginating a filtered set it cannot express in SQL.
        """
        query = self.db.query(ProductFamilyModel)
        if search:
            query = query.filter(ProductFamilyModel.name.ilike(f"%{search}%"))
        query = self._apply_sort(query, sort, ProductFamilyModel.name)

        if issues_only:
            # Coverage is not a SQL predicate (see ``compute_family_stats``), so
            # the only correct way to page "families with a problem" is to score
            # them all and then page. 75 families x ~5 members is nothing.
            families = query.all()
            stats = compute_family_stats(
                self._members_by_family([f.id for f in families])
            )
            flagged = [
                (f, stats[f.id])
                for f in families
                if stats[f.id].has_no_boards
                or stats[f.id].has_no_edge_bandings
                or stats[f.id].uncovered_thicknesses
                or len(stats[f.id].aliases) > 1
                or stats[f.id].missing_alias_count
            ]
            return flagged[offset : offset + limit], len(flagged)

        families, total = self._paginate(query, limit, offset)
        stats = compute_family_stats(self._members_by_family([f.id for f in families]))
        return [(f, stats[f.id]) for f in families], total

    def detail(
        self, family_id: int
    ) -> Tuple[ProductFamilyModel, FamilyStats, List[ProductModel], List[ProductModel]]:
        """One family with its stats and its members split by side."""
        family = self.get_or_404(family_id)
        members = self._members_by_family([family.id])[family.id]
        stats = compute_family_stats({family.id: members})[family.id]
        boards = sorted(
            (p for p in members if p.type == ProductType.BOARD.value),
            key=lambda p: p.name,
        )
        bandings = sorted(
            (p for p in members if p.type == ProductType.EDGE_BANDING.value),
            key=lambda p: (p.attributes.get("width", 0), p.name),
        )
        return family, stats, boards, bandings

    def assign(self, family_id: Optional[int], product_ids: Sequence[int]) -> int:
        """(Re)assigns products to a family, or to none when ``family_id`` is None.

        All-or-nothing on the ids: an unknown one raises before anything is
        written. On a bulk-assignment screen a silent partial write is the worst
        possible outcome — the operator believes they moved 40 products and moved
        37, with nothing saying which three are missing.

        Goes through the ORM rather than a bulk ``UPDATE`` on purpose:
        ``CRUDService._stamp_actor`` only runs inside ``_persist``, so a
        statement-level update would silently drop ``updated_by``/``updated_at``
        on the largest edit anybody makes in this system.
        """
        if family_id is not None and self.db.get(ProductFamilyModel, family_id) is None:
            raise EntityNotFoundError("ProductFamily", family_id)

        wanted = list(dict.fromkeys(product_ids))
        products = self.db.query(ProductModel).filter(ProductModel.id.in_(wanted)).all()
        found = {p.id for p in products}
        missing = [pid for pid in wanted if pid not in found]
        if missing:
            raise EntityNotFoundError("Product", missing[0])

        for product in products:
            product.family_id = family_id
            self._stamp_actor(product)
        self.db.commit()
        return len(products)

    def set_alias(self, family_id: int, alias: str, product_ids: Sequence[int]) -> int:
        """Stamps one printed code onto several of the family's tapes.

        Scoped to the family on purpose: this is reached from the screen that
        REPORTS the missing alias, so the ids it sends are its own members, and
        anything else arriving here is a mistake worth refusing rather than
        obeying. A board is refused for the same reason the column has a CHECK —
        an alias is an edge-banding field.

        All-or-nothing, like ``assign``: a partial write on a bulk screen leaves
        the operator believing they fixed five tapes when they fixed three.
        """
        family = self.get_or_404(family_id)

        wanted = list(dict.fromkeys(product_ids))
        products = self.db.query(ProductModel).filter(ProductModel.id.in_(wanted)).all()
        found = {p.id for p in products}
        missing = [pid for pid in wanted if pid not in found]
        if missing:
            raise EntityNotFoundError("Product", missing[0])

        for product in products:
            if product.type != ProductType.EDGE_BANDING.value:
                raise ValidationError(
                    f"El producto {product.code} no es un tapacanto: no lleva alias"
                )
            if product.family_id != family.id:
                raise ValidationError(
                    f"El producto {product.code} no pertenece a la familia "
                    f"'{family.name}'"
                )

        for product in products:
            product.alias = alias
            self._stamp_actor(product)
        self.db.commit()
        return len(products)


def product_family_service(db: Session = Depends(get_db)) -> ProductFamilyService:
    """``ProductFamilyService`` provider for route injection."""
    return ProductFamilyService(db)
