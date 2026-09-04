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


# The three sale prices the catalog carries per product, by the number the API
# takes. Fixed in code on purpose: they are not a configurable list of discounts
# any more but three columns the vendor's inventory publishes, so adding a
# fourth would be a schema change, not a settings edit. Level 1 is the list
# price everything falls back to.
PRICE_LEVEL_NAMES = {1: "Precio 1", 2: "Precio 2", 3: "Precio 3"}


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
    alias: Optional[str] = Field(
        default=None,
        description=(
            "Short code printed in the workshop notation, e.g. 'CSH'. "
            "Independent of the board↔tapacanto coordination field `family`."
        ),
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
    """Money block for a quote or an order: net subtotal, tax, total.

    Every line item is already priced at the level the seller chose (the boards
    they marked; see ``applyPriceLevel``), so there is no discount row: the
    subtotal IS the sum of what the document prints. Catalog prices are net, and
    additional services — which staff registers tax-included — are converted to
    net here, so one tax line covers the whole document.
    """

    price_level: int = Field(
        default=1, ge=1, le=3, description="Price level applied to the marked boards"
    )
    price_level_name: Optional[str] = Field(default=None)
    subtotal: float = Field(
        default=0.0, description="Net sum (boards + edge banding + services)"
    )
    services_total: float = Field(
        default=0.0, description="Net sum of the additional services"
    )
    tax_rate: float = Field(default=0.0, description="Applied tax rate (0.15 = 15%)")
    tax_amount: float = Field(default=0.0, description="Tax over the subtotal")
    total: float = Field(default=0.0, description="Subtotal plus tax")


class AdditionalServiceLine(CamelModel):
    """A billed additional service on a quote/order (qty × editable unit price).

    Not cut geometry: it lives beside the optimizer inputs and is folded into the
    total **after** the cache-keyed computation (like the price level). It never
    feeds the optimizer. ``service_id`` references the catalog (optional; the price
    is editable regardless of the catalog default).

    ``unit_price`` is registered **tax-included**, unlike the catalog's net
    prices: it is a number staff types from a price list, not one the vendor's
    inventory publishes. ``build_pricing`` converts it to net so the document's
    single tax line covers services too.
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
    apply_price_level: bool = Field(
        default=False,
        description=(
            "Whether this board is billed at the quote's `priceLevel` instead of "
            "the list price. Defaults to false: the seller marks board by board "
            "which ones get the reduced price when quoting (a client negotiates "
            "the melamina and not the MDF). Does not affect optimization geometry "
            "or hash (like clientId/priceLevel); only what each line costs."
        ),
    )
    whole_board: bool = Field(
        default=False,
        description=(
            "Whether a sheet the optimizer billed as a half board is delivered "
            "(and charged) as a whole board, the client keeping the uncut half. "
            "Does not affect the search or the hash: the cached plan is reshaped "
            "afterwards, so the pieces stay exactly where they were and the "
            "untouched half becomes one clean leftover plus the rip cut that "
            "separates it. No-op for a material the optimizer didn't halve."
        ),
    )


class InlineMaterialInput(CamelModel):
    """Material with inline dimensions: company/client offcut or manual measurement.

    They share the same shape; only ``source`` differs — and that difference is
    what decides supply. An offcut (``companyOffcut``/``clientOffcut``) is a
    physical piece somebody owns, so ``quantity`` is **always** finite supply
    (default 1), whether it hangs off another material or is referenced directly
    by requirements. ``manual`` models a board *type* the seller measured by hand
    rather than a unique piece, so it stays infinite.

    A client offcut is the client's own material and never carries a price: the
    cost is coerced to 0 rather than rejected, because pre-orders re-validate
    this model on every read and a 422 there would surface as a 500.
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
            "Available units (finite supply). Enforced for `companyOffcut` and "
            "`clientOffcut`, where it defaults to 1: the client brings two "
            "retazos, not an unlimited supply of them. Ignored for `manual`, "
            "which is a board type rather than a physical piece."
        ),
    )
    pool_key: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "If set, this offcut is extra stock of the material with this `key` "
            "(its *anchor*): the pieces come from the anchor's requirements and "
            "the optimizer packs them across anchor + offcuts. The anchor is "
            "usually a catalog board, but it may be another offcut — that is how "
            "a job cut only on the client's retazos is expressed. A pooled offcut "
            "is NOT referenced by any requirement."
        ),
    )

    @model_validator(mode="after")
    def _client_material_is_free(self) -> "InlineMaterialInput":
        """A client offcut is the client's own material: it never has a price.

        Coerced, not rejected: pre-orders store this payload and re-validate it
        on every read (``preorders.service.build_request``), so raising here
        would turn an already-saved quote into a 500.
        """
        if self.source == MaterialSource.client_offcut:
            self.cost_per_unit = 0.0
        return self


# Union discriminated by ``source`` (same pattern as ``products/schemas.py``):
# Pydantic v2 picks and validates the branch based on the ``source`` sent. A new
# source = a value in ``MaterialSource`` + its branch here (or reuse the inline branch).
MaterialInput = Annotated[
    Union[CatalogMaterialInput, InlineMaterialInput],
    Field(discriminator="source"),
]


def _field(item, name: str):
    """Reads a field off a model or off its stored ``model_dump`` dict.

    Pre-orders persist these lists as JSON and edit them in place, so the same
    rules have to run against plain dicts too — snake_case, since ``model_dump``
    without ``by_alias`` keeps the field names.
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def validate_material_graph(materials: list, requirements: list) -> None:
    """Cross-field checks over a materials+requirements pair.

    At module level because pre-orders and orders persist the same two lists:
    keeping the rules on ``OptimizeRequest`` alone let an inconsistent set be
    *saved* and blow up only later, inside ``build_request``, as a 500 — a
    pre-order that could no longer be opened, quoted or confirmed.
    """
    keys = [_field(m, "key") for m in materials]
    if len(set(keys)) != len(keys):
        raise ValueError("material keys must be unique")
    by_key = {_field(m, "key"): m for m in materials}

    # Pooled materials (``pool_key`` set) are extra stock of their anchor, not a
    # direct cut target: their pieces come from the anchor's requirements.
    pooled_keys = {_field(m, "key") for m in materials if _field(m, "pool_key")}

    for m in materials:
        pool_key = _field(m, "pool_key")
        if pool_key is None:
            continue
        if pool_key not in by_key:
            raise ValueError(
                f"material '{_field(m, 'key')}' poolKey references unknown "
                f"material '{pool_key}'"
            )
        # No chains: the anchor is the material the requirements point at, so it
        # cannot itself be stock of a third one. It may be a catalog board (board
        # + retazos) or an offcut (retazos only) — that is the whole point.
        if pool_key in pooled_keys:
            raise ValueError(
                f"material '{_field(m, 'key')}' poolKey references pooled "
                f"material '{pool_key}'; point it at the material the pieces "
                f"belong to"
            )

    for req in requirements:
        material_key = _field(req, "material_key")
        if material_key not in by_key:
            raise ValueError(
                f"requirement references unknown materialKey '{material_key}'"
            )
        if material_key in pooled_keys:
            raise ValueError(
                f"requirement cannot reference pooled material "
                f"'{material_key}'; reference its anchor material instead"
            )


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
    price_level: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Price level to bill the marked boards at: 1 (list) | 2 | 3. The "
            "three prices come from the catalog per product, so this is a "
            "different unit price rather than a percentage off. Does not affect "
            "optimization geometry or hash; only what the marked lines cost."
        ),
    )
    strategy: OptimizationStrategy = Field(
        default=OptimizationStrategy.default,
        description=(
            "Packing heuristic. `default`: maximum efficiency (minimizes total "
            "waste). `longOffcuts`: concentrates waste into one long reusable "
            "strip by pushing pieces to one side. DOES affect optimization "
            "geometry and hash (unlike clientId/priceLevel)."
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
        validate_material_graph(self.materials, self.requirements)
        return self

    @property
    def leveled_material_keys(self) -> set:
        """Keys of the catalog boards the seller marked for the price level.

        The single place that reads ``applyPriceLevel``, so the three callers of
        the re-pricing pass (raw optimize, pre-order, order) can't drift apart.
        Inline materials (offcut/manual) don't carry the flag: their cost comes
        from the request, not from a catalog that has levels.
        """
        return {m.key for m in self.materials if getattr(m, "apply_price_level", False)}

    @property
    def whole_board_material_keys(self) -> set:
        """Keys of the catalog boards the client takes whole, half or not.

        The single place that reads ``wholeBoard``, mirroring
        ``leveled_material_keys``: both are commercial flags kept out of
        ``_compute_hash`` so a checkbox reshapes/re-prices the cached plan
        instead of re-running the search. Inline materials (offcut/manual) don't
        carry the flag — they are never halved in the first place.
        """
        return {m.key for m in self.materials if getattr(m, "whole_board", False)}


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


class UnplacedPiece(CamelModel):
    """A piece the available stock could not hold.

    Only reachable when supply is finite — a catalog board is unlimited, so a
    quote anchored on one only lists a piece here when it is larger than the
    board itself. A job cut on the client's retazos, on the other hand, can run
    out of material, and saying so is the whole point: the seller then raises the
    retazo count, attaches a board, or drops the piece.
    """

    material_key: str
    label: Optional[str] = None
    height: float
    width: float
    quantity: int = Field(..., description="How many instances did not fit")


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
    unplaced: List[UnplacedPiece] = Field(
        default_factory=list,
        description=(
            "Pieces that did not fit the available stock, grouped by size. Empty "
            "on every catalog-anchored quote; populated when a pool of finite "
            "offcuts runs out. The plan returned is still valid for everything "
            "else — these are the pieces it does NOT cut."
        ),
    )
