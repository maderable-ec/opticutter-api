from enum import Enum
from typing import Annotated, List, Literal, Optional, Union

from pydantic import (
    Field,
    NonNegativeInt,
    PositiveInt,
    confloat,
    field_validator,
    model_validator,
)

from src.cutting import PackingStrategy
from src.modules.clients.schemas import ClientResponse
from src.shared.schemas import CamelModel


class MaterialSource(str, Enum):
    """Source of the material to optimize.

    The cutting engine is source-agnostic: it only needs dimensions and cost.
    ``catalog`` resolves a board from the product catalog; the rest provide
    their dimensions inline. A new source = one more value + its branch in the
    union.
    """

    catalog = "catalog"
    company_offcut = "companyOffcut"
    client_offcut = "clientOffcut"
    manual = "manual"


class OptimizationStrategy(str, Enum):
    """Packing heuristic to apply during optimization.

    ``default`` (Best-Area-Fit) minimizes total waste but fragments it across
    several offcuts. ``longOffcuts`` pushes pieces against one side of the
    board and concentrates the waste into one long continuous strip (along the
    board's long axis), reusable as an offcut. Maps to ``cutting.PackingStrategy``.
    """

    default = "default"
    long_offcuts = "longOffcuts"


# Translation from the API enum to the cutting domain's profile.
STRATEGY_TO_PACKING = {
    OptimizationStrategy.default: PackingStrategy.MAX_EFFICIENCY,
    OptimizationStrategy.long_offcuts: PackingStrategy.LONG_OFFCUTS,
}


class PoolFillOrder(str, Enum):
    """Fill order for a material pool (a catalog board + its attached offcuts).

    Only relevant when a catalog board carries pooled offcuts (inline materials
    whose ``pool_key`` points at it). ``auto`` computes both candidate packings
    and keeps the one with the least waste on the *purchased* (catalog) sheets;
    ``offcuts_first`` fills the client's offcuts before opening catalog boards;
    ``catalog_first`` fills catalog boards and pushes the residual onto the
    offcuts (so a big leftover lands on the client's offcut, not a bought board).
    """

    auto = "auto"
    offcuts_first = "offcutsFirst"
    catalog_first = "catalogFirst"


class MaterialSummary(CamelModel):
    material_key: str
    source: MaterialSource
    product_id: Optional[int] = None
    product_code: Optional[str] = None
    product_name: Optional[str] = None
    height: float
    width: float
    thickness: float
    count: int
    total_area_m2: float
    avg_efficiency: float
    cost_per_unit: float
    total_cost: float
    half_board: bool = Field(
        default=False,
        description="True if this line is a half board (length kept, width/2, cost/2)",
    )


class EdgeSide(str, Enum):
    """Nominal sides of a piece (unrotated frame).

    ``top``/``bottom`` are the sides of length ``width``; ``left``/``right``
    are the sides of length ``height``.
    """

    top = "top"
    bottom = "bottom"
    left = "left"
    right = "right"


class EdgeBandingSpec(CamelModel):
    """Edge banding to apply to a piece: the sides to band and, optionally, the product.

    At optimize time ``sides`` is enough to compute the edge-banding length
    (linear meters) — which is what matters for cuts and length. The
    ``productId`` (price + soft/hard type + color for the diagram) is only
    assigned when quoting; that's why it's optional here.
    """

    sides: List[EdgeSide] = Field(
        ...,
        min_length=1,
        description="Nominal sides to band (top/bottom=ancho, left/right=alto)",
    )
    product_id: Optional[int] = Field(
        default=None,
        description=(
            "Edge banding product ID (type=edge_banding). Optional: omit at optimize "
            "time (geometry only); assigned when quoting to price the banding."
        ),
    )

    @field_validator("sides")
    @classmethod
    def _unique_sides(cls, sides: List[EdgeSide]) -> List[EdgeSide]:
        if len(set(sides)) != len(sides):
            raise ValueError("sides must not contain duplicates")
        return sides


class EdgeBandingSummary(CamelModel):
    # product_* and thickness stay optional: a banded piece without an assigned
    # product (sides only, at optimize time) contributes length but no identity or price.
    product_id: Optional[int] = None
    product_code: Optional[str] = None
    product_name: Optional[str] = None
    thickness: Optional[float] = None
    color: Optional[str] = None
    band_type: Optional[str] = Field(
        default=None, description="Canonical band type: 'Soft' / 'Hard'"
    )
    family: Optional[str] = Field(
        default=None,
        description="Design family printed in the workshop notation, e.g. 'CSH'",
    )
    net_linear_m: float = Field(
        ..., description="Net linear meters (sum of banded sides)"
    )
    linear_m: float = Field(..., description="Linear meters including waste factor")
    billed_linear_m: float = Field(
        ..., description="Linear meters charged: net + waste factor, not rounded"
    )
    price_per_m: float = Field(..., description="Frozen price per linear meter")
    total_cost: float


class PricingSummary(CamelModel):
    """Document-level discount block (applied price tier).

    Line items are charged at list price ("Precio Consumidor"); ``discountAmount``
    is the single adjustment, computed only over the catalog boards (``discountBase``).
    """

    price_tier_code: Optional[str] = Field(
        default=None, description="Code of the applied price tier"
    )
    price_tier_name: Optional[str] = Field(default=None)
    discount_rate: float = Field(
        default=0.0, description="Applied discount (0.02 = 2%)"
    )
    discount_base: float = Field(
        default=0.0, description="Discount base: catalog boards"
    )
    subtotal: float = Field(default=0.0, description="Sum at list price")
    discount_amount: float = Field(default=0.0)
    services_total: float = Field(
        default=0.0,
        description="Sum of additional services (added after the discount)",
    )
    total: float = Field(
        default=0.0, description="Subtotal minus discount plus additional services"
    )


class AdditionalServiceLine(CamelModel):
    """A billed additional service on a quote/order (qty × editable unit price).

    Not cut geometry: it lives beside the optimizer inputs and is folded into the
    total **after** the cache-keyed computation (like the tier discount). It never
    feeds the optimizer. ``service_id`` references the catalog (optional; the price
    is editable regardless of the catalog default).
    """

    service_id: Optional[int] = Field(
        default=None, description="Additional service catalog ID (optional)"
    )
    name: str = Field(
        ..., min_length=1, max_length=128, description="Service name (snapshot)"
    )
    unit_price: confloat(ge=0) = Field(
        ..., description="Unit price (seeded from the catalog default, editable)"
    )
    quantity: PositiveInt = Field(default=1, le=10000, description="Quantity")


class CatalogMaterialInput(CamelModel):
    """Material from the product catalog (board)."""

    key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Stable key referenced by requirements via materialKey",
    )
    source: Literal[MaterialSource.catalog]
    product_id: int = Field(..., description="Board product ID (type=board)")
    fill_order: PoolFillOrder = Field(
        default=PoolFillOrder.auto,
        description=(
            "Fill order when this board has attached offcuts (materials whose "
            "`poolKey` points at this board's `key`). `auto` picks the least-waste "
            "layout; `offcutsFirst`/`catalogFirst` force the direction. Ignored "
            "when the board has no pooled offcuts. Affects geometry and the hash."
        ),
    )


class InlineMaterialInput(CamelModel):
    """Material with inline dimensions: company/client offcut or manual measurement.

    They share the same shape; only ``source`` differs. ``quantity`` is enforced
    as finite supply **only** when the material is a pooled offcut (``pool_key``
    set); as a standalone material referenced by requirements it stays infinite.
    """

    key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Stable key referenced by requirements via materialKey",
    )
    source: Literal[
        MaterialSource.company_offcut,
        MaterialSource.client_offcut,
        MaterialSource.manual,
    ]
    height: PositiveInt = Field(..., description="Material height (alto) in mm")
    width: PositiveInt = Field(..., description="Material width (ancho) in mm")
    thickness: PositiveInt = Field(..., description="Material thickness in mm")
    cost_per_unit: confloat(ge=0) = Field(
        default=0.0, description="Unit cost of the material (0 if unknown)"
    )
    label: Optional[str] = Field(
        default=None, max_length=128, description="Human-friendly material label"
    )
    quantity: Optional[PositiveInt] = Field(
        default=None,
        description=(
            "Available units (finite supply). Enforced when this is a pooled "
            "offcut (`poolKey` set); defaults to 1 in that case. Ignored for a "
            "standalone material referenced directly by requirements."
        ),
    )
    pool_key: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "If set, this offcut is extra stock of the catalog board with this "
            "`key`: its pieces come from that board's requirements, and the "
            "optimizer packs them across the board + these offcuts. A pooled "
            "offcut is NOT referenced by any requirement."
        ),
    )


# Union discriminated by ``source`` (same pattern as ``products/schemas.py``):
# Pydantic v2 picks and validates the branch based on the ``source`` sent. A new
# source = a value in ``MaterialSource`` + its branch here (or reuse the inline branch).
MaterialInput = Annotated[
    Union[CatalogMaterialInput, InlineMaterialInput],
    Field(discriminator="source"),
]


class Requirement(CamelModel):
    priority: NonNegativeInt = Field(
        ..., description="Cutting priority; higher values are placed first"
    )
    height: PositiveInt = Field(
        ..., description="Piece height (alto, primera medida) in mm"
    )
    width: PositiveInt = Field(
        ..., description="Piece width (ancho, segunda medida) in mm"
    )
    quantity: PositiveInt = Field(default=1, le=10000)
    material_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Key of the material (from `materials`) to cut this piece from",
    )
    label: Optional[str] = Field(default=None, description="Human-friendly piece label")
    can_rotate: bool = Field(
        default=True,
        description=(
            "If true, the optimizer may swap height↔width (rotate 90°) to improve "
            "yield. Set false for pieces with a fixed orientation (grain/pattern). "
            "Edge banding is remapped to the rotated sides, so it does not block "
            "rotation."
        ),
    )
    edge_banding: Optional[EdgeBandingSpec] = Field(
        default=None, description="Optional edge banding for this piece"
    )


class OptimizeRequest(CamelModel):
    materials: List[MaterialInput] = Field(
        ...,
        min_length=1,
        description="Available materials (stock): catalog boards, offcuts or manual",
    )
    requirements: List[Requirement] = Field(
        ..., min_length=1, description="List of cuts to optimize"
    )
    client_id: Optional[int] = Field(
        default=None,
        description=(
            "Optional client ID. The optimization is client-agnostic (the result "
            "and its hash do not depend on the client); only proformas and orders "
            "require a client, resolved at that point."
        ),
    )
    price_tier_code: Optional[str] = Field(
        default="consumidor",
        max_length=32,
        description=(
            "Price tier for the proforma: consumidor (0%) | carpintero (2%) | "
            "efectivo (5%). Does not affect optimization geometry or hash; only "
            "the `pricing` block (discount over catalog boards)."
        ),
    )
    strategy: OptimizationStrategy = Field(
        default=OptimizationStrategy.default,
        description=(
            "Packing heuristic. `default`: maximum efficiency (minimizes total "
            "waste). `longOffcuts`: concentrates waste into one long reusable "
            "strip by pushing pieces to one side. DOES affect optimization "
            "geometry and hash (unlike clientId/priceTierCode)."
        ),
    )
    variant: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Alternative-solution seed. 0 = canonical solution; each increment "
            "reorders the search exploration to produce a genuinely different "
            "layout when alternatives exist. DOES affect geometry and hash, so "
            "every variant is cached and deterministic on its own."
        ),
    )

    @model_validator(mode="after")
    def _validate_material_refs(self) -> "OptimizeRequest":
        """Keys are unique; requirements and pool links resolve consistently."""
        keys = [m.key for m in self.materials]
        if len(set(keys)) != len(keys):
            raise ValueError("material keys must be unique")
        by_key = {m.key: m for m in self.materials}

        # Pooled offcuts (``pool_key`` set) are extra stock of a catalog board,
        # not a direct cut target: their pieces come from that board's pool.
        pooled_keys = set()
        for m in self.materials:
            pool_key = getattr(m, "pool_key", None)
            if pool_key is None:
                continue
            pooled_keys.add(m.key)
            target = by_key.get(pool_key)
            if target is None:
                raise ValueError(
                    f"material '{m.key}' poolKey references unknown material "
                    f"'{pool_key}'"
                )
            if target.source != MaterialSource.catalog:
                raise ValueError(
                    f"material '{m.key}' poolKey must reference a catalog board, "
                    f"not '{pool_key}'"
                )

        for req in self.requirements:
            if req.material_key not in by_key:
                raise ValueError(
                    f"requirement references unknown materialKey '{req.material_key}'"
                )
            if req.material_key in pooled_keys:
                raise ValueError(
                    f"requirement cannot reference pooled offcut "
                    f"'{req.material_key}'; reference its catalog board instead"
                )
        return self


class Material(CamelModel):
    material_key: str = Field(
        ..., description="Key of the material (from `materials`) this sheet came from"
    )
    sheet_number: int = Field(
        ..., description="Sheet number within the board (1-based)"
    )
    height: float = Field(..., description="Height of the material (alto)")
    width: float = Field(..., description="Width of the material (ancho)")
    thickness: float = Field(..., description="Thickness of the material")
    area: float = Field(..., description="Area of the material")
    half_board: bool = Field(
        default=False,
        description="True if this sheet is a half board (length kept, width/2, cost/2)",
    )


class PlacedPiece(CamelModel):
    piece_id: str = Field(..., description="Unique identifier for the placed piece")
    x: float = Field(..., description="X position of the placed piece")
    y: float = Field(..., description="Y position of the placed piece")
    height: float = Field(
        ..., description="Height of the placed piece (alto, after rotation)"
    )
    width: float = Field(
        ..., description="Width of the placed piece (ancho, after rotation)"
    )
    rotated: bool = Field(..., description="Indicates if the piece is rotated")
    original_height: float = Field(
        ..., description="Piece height (alto) before rotation"
    )
    original_width: float = Field(
        ..., description="Piece width (ancho) before rotation"
    )
    edges: Optional[dict] = Field(
        default=None,
        description="Edge banding on the geometric sides of the placed piece",
    )


class Remainder(CamelModel):
    x: float = Field(..., description="X position of the remainder")
    y: float = Field(..., description="Y position of the remainder")
    height: float = Field(..., description="Height of the remainder (alto)")
    width: float = Field(..., description="Width of the remainder (ancho)")


class CutSegment(CamelModel):
    """Guillotine cut segment (saw travel) on the sheet."""

    x: float = Field(..., description="X where the saw cut starts")
    y: float = Field(..., description="Y where the saw cut starts")
    length: float = Field(..., description="Length of the saw travel along its axis")
    is_horizontal: bool = Field(
        ..., description="True if the cut runs horizontally (along X)"
    )


class LayoutStatistics(CamelModel):
    used_area: float = Field(..., description="Total area occupied by placed pieces")
    waste_area: float = Field(..., description="Unused area of the sheet")
    efficiency: float = Field(..., description="Material usage efficiency (percentage)")
    pieces_count: int = Field(..., description="Number of pieces placed on the sheet")
    cut_linear_m: float = Field(
        default=0.0, description="Linear meters of cut (saw travel) for this sheet"
    )
    edge_banding_linear_m: float = Field(
        default=0.0,
        description="Net linear meters of edge banding on this sheet (informational)",
    )


class Layout(CamelModel):
    material: Material = Field(..., description="Material/sheet used in this layout")
    placed_pieces: List[PlacedPiece] = Field(
        ..., description="Pieces placed on this sheet"
    )
    statistics: LayoutStatistics = Field(
        ..., description="Usage metrics for this sheet"
    )
    remainders: List[Remainder] = Field(
        ..., description="Leftover rectangles on this sheet"
    )
    # Empty default: cached payloads/snapshots predating this key still validate.
    cuts: List[CutSegment] = Field(
        default_factory=list,
        description="Guillotine saw cuts on this sheet (for drawing cut lines)",
    )


class LayoutGroup(CamelModel):
    pattern_id: int = Field(..., description="1-based index of the unique cut pattern")
    count: int = Field(..., description="Number of sheets sharing this pattern")
    sheet_numbers: List[int] = Field(
        ..., description="Sheet numbers that use this pattern"
    )
    material_key: str = Field(
        ..., description="Key of the material the pattern is cut from"
    )
    layout: Layout = Field(..., description="Representative layout for this pattern")


class OptimizeResponse(CamelModel):
    id: Optional[int] = Field(
        default=None,
        description="Deprecated: optimizations are no longer persisted; use the hash",
    )
    client: Optional[ClientResponse] = Field(
        default=None, description="Client information (only when a client_id is sent)"
    )
    optimization_hash: Optional[str] = Field(
        default=None, description="Deterministic hash of the optimization inputs"
    )
    strategy: OptimizationStrategy = Field(
        default=OptimizationStrategy.default,
        description="Packing heuristic applied to this optimization",
    )
    variant: int = Field(
        default=0,
        description="Alternative-solution seed this result was computed with",
    )
    total_boards_used: int = Field(..., description="Total number of boards used")
    total_boards_cost: float = Field(..., description="Total cost of boards used")
    total_edge_banding_cost: float = Field(
        default=0.0, description="Total cost of edge banding used"
    )
    total_cut_linear_m: float = Field(
        default=0.0, description="Total linear meters of cut across all sheets"
    )
    total_edge_banding_linear_m: float = Field(
        default=0.0,
        description="Total net linear meters of edge banding across all sheets",
    )
    layouts: List[Layout] = Field(
        ..., description="Per-sheet cutting layouts of the optimization"
    )
    materials_summary: Optional[List[MaterialSummary]] = Field(
        default=None, description="Aggregated materials grouped by board type"
    )
    edge_bandings_summary: Optional[List[EdgeBandingSummary]] = Field(
        default=None, description="Aggregated edge banding grouped by type"
    )
    layout_groups: Optional[List[LayoutGroup]] = Field(
        default=None, description="Cutting layouts deduplicated by identical pattern"
    )
    pricing: Optional[PricingSummary] = Field(
        default=None,
        description="Discount block for the selected price tier (document-level)",
    )
