"""Schemas for the design families that coordinate boards with edge bandings.

Kept in their own module rather than folded into ``products/schemas.py`` because
the coverage diagnostic pulls in a fair amount of shape that has nothing to do
with a product. ``ProductFamilyRef`` is defined in ``schemas.py`` and
re-exported here, so the dependency runs one way only.
"""

from typing import List, Optional

from pydantic import Field

from src.modules.products.schemas import ProductFamilyRef, ProductResponse
from src.shared.schemas import CamelModel

__all__ = [
    "FamilyAliasRequest",
    "FamilyAliasResult",
    "FamilyAssignRequest",
    "FamilyAssignResult",
    "ProductFamilyCreate",
    "ProductFamilyDetailResponse",
    "ProductFamilyRef",
    "ProductFamilyResponse",
    "ProductFamilyUpdate",
]


class ProductFamilyBase(CamelModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Nombre del diseño, tal como se escribe (p. ej. «Olmo Panela»)",
    )
    description: Optional[str] = Field(
        None,
        max_length=256,
        description="Por qué existe o a qué sustituye, para quien herede el catálogo",
    )


class ProductFamilyCreate(ProductFamilyBase):
    pass


class ProductFamilyUpdate(CamelModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    description: Optional[str] = Field(None, max_length=256)


class ProductFamilyResponse(ProductFamilyBase):
    """A family plus everything the management screen needs to triage it.

    The counts and the signals are computed over ACTIVE products only: an
    inactive product coordinates with nothing, so counting it would show a
    family as healthy while its picker comes back empty.
    """

    id: int
    board_count: int = Field(0, description="Tableros activos en la familia")
    edge_banding_count: int = Field(0, description="Tapacantos activos en la familia")
    # The two signals that used to be catalog-sync warnings. They moved here
    # because coordination stopped living in the vendor's ``obs``: computed from
    # source rows they would flag families we already fixed by hand and miss the
    # ones we break.
    has_no_boards: bool = Field(
        False, description="Solo tiene tapacantos: nada coordina con ellos"
    )
    has_no_edge_bandings: bool = Field(
        False, description="Solo tiene tableros: su selector sale vacío"
    )
    uncovered_thicknesses: List[float] = Field(
        default_factory=list,
        description=(
            "Espesores de tablero de la familia que ningún ancho de sus "
            "tapacantos alcanza a cubrir (la ventana de solape de 1 a 10 mm)"
        ),
    )
    # Distinct non-null aliases among the family's tapes. More than one means the
    # thermal label prints two codes for what the catalog says is one design —
    # zero cases today, and worth keeping at zero.
    aliases: List[str] = Field(default_factory=list)
    missing_alias_count: int = Field(
        0, description="Tapacantos de la familia sin alias: imprimen sin código"
    )

    @property
    def has_issues(self) -> bool:  # pragma: no cover - convenience for callers
        return bool(
            self.has_no_boards
            or self.has_no_edge_bandings
            or self.uncovered_thicknesses
            or len(self.aliases) > 1
            or self.missing_alias_count
        )


class ProductFamilyDetailResponse(ProductFamilyResponse):
    """The family plus its members, split by what each side is for."""

    boards: List[ProductResponse] = []
    edge_bandings: List[ProductResponse] = []


class FamilyAssignRequest(CamelModel):
    """Bulk (re)assignment. ``familyId`` null unassigns.

    One endpoint for both directions on purpose: unassigning IS assigning to
    NULL, and two endpoints would duplicate the id validation and the audit
    stamping. It is a POST so it can never collide with
    ``/{family_id}`` — no other POST on this router takes a path parameter.
    """

    family_id: Optional[int] = Field(
        None, description="Familia destino; null para desasignar"
    )
    product_ids: List[int] = Field(
        ..., min_length=1, description="Productos a (re)asignar"
    )


class FamilyAliasRequest(CamelModel):
    """Sets the printed short code on several of a family's tapes at once.

    The alias belongs to the TAPE, not to the design — that stayed deliberate —
    but in practice every tape of one design prints the same code (measured: 72
    families, zero with a divergent alias). So the case this serves is a sync
    bringing in new widths of a design that already has a code, and the operator
    giving them all that code in one move instead of opening five product forms.
    """

    alias: str = Field(
        ..., min_length=1, max_length=20, description="Código corto a imprimir"
    )
    product_ids: List[int] = Field(
        ..., min_length=1, description="Tapacantos de la familia a actualizar"
    )


class FamilyAliasResult(CamelModel):
    updated: int
    alias: str


class FamilyAssignResult(CamelModel):
    assigned: int = Field(..., description="Productos efectivamente actualizados")
    family_id: Optional[int] = None
