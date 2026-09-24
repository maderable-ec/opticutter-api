from enum import Enum
from typing import Annotated, ClassVar, Dict, List, Literal, Optional, Tuple, Union

from pydantic import (
    Field,
    NonNegativeInt,
    PositiveInt,
    SerializerFunctionWrapHandler,
    confloat,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from src.modules.clients.schemas import ClientResponse
from src.modules.optimizations.unplaced import UnplacedReason
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
        description=(
            "True if this line is a half board: half the sheet at cost/2 plus "
            "markup. Which side is halved depends on the material (the largo "
            "for most, the lado corto for plywood and ranurado); read the "
            "width/height, don't assume."
        ),
    )
    skip_trim: bool = Field(
        default=False,
        description="True if these sheets are cut without the configured trim margins",
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


class SpecialEdge(CamelModel):
    """A canto especial: one side of the piece banded with a tape of its own.

    What the seller types as ``2L CS BLN`` (the auto banding's own notation)
    arrives resolved to one entry per side, each with its product: the band
    type and the alias are the PRODUCT's, read from the catalog when the
    payload is built, never stored here. Unlike ``EdgeBandingSpec.product_id``
    the product is mandatory -- a special edge exists precisely to name a tape.
    """

    side: EdgeSide = Field(..., description="Nominal side (top/bottom/left/right)")
    product_id: int = Field(
        ..., gt=0, description="Edge banding product ID (type=edge_banding)"
    )


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
    they marked; see ``applyPriceLevel``), so no total is derived from a
    discount: the subtotal IS the sum of what the document prints.
    ``discountAmount``/``listSubtotal`` sit beside it as the seller's argument —
    what the same cut list would have cost at the list price. Catalog prices are net, and
    additional services — which staff registers tax-included — are converted to
    net here, so one tax line covers the whole document.
    """

    price_level: int = Field(
        default=1, ge=1, le=3, description="Price level applied to the marked boards"
    )
    price_level_name: Optional[str] = Field(default=None)
    discount_amount: float = Field(
        default=0.0,
        description=(
            "How far below the list price (level 1) the marked boards landed. "
            "Informative: no line and no total is derived from it, and it is "
            "0.0 at level 1 or when nothing was marked."
        ),
    )
    list_subtotal: float = Field(
        default=0.0,
        description="What the same document would cost with no level applied",
    )
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
    skip_trim: bool = Field(
        default=False,
        description=(
            "Whether this board is cut WITHOUT the trim margins configured in "
            "settings, so the sheet is used edge to edge. The shop squares every "
            "board by default, but some cut lists don't need it and the seller "
            "decides that per job. Applies to the whole pool: this board and "
            "every offcut whose `poolKey` points at it. Unlike `applyPriceLevel`/"
            "`wholeBoard`, this one DOES move the geometry, so it is part of the "
            "hash and re-runs the search."
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

    skip_trim: bool = Field(
        default=False,
        description=(
            "Whether this material is cut WITHOUT the trim margins configured in "
            "settings. Same flag as the catalog board's: a client's retazo usually "
            "arrives already squared, and a pool can be anchored on one. Read from "
            "the pool's ANCHOR only — on a pooled offcut it is coerced to false, "
            "since the anchor's setting already covers the whole pool. Affects "
            "geometry and the hash."
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

    @model_validator(mode="after")
    def _only_the_anchor_decides_the_trim(self) -> "InlineMaterialInput":
        """``skipTrim`` belongs to the pool, so a pooled offcut never carries it.

        The service reads the flag off the anchor alone (the material the
        requirements point at) and hands ONE ``CuttingParameters`` to the whole
        pool. Left alone, the flag on a pooled offcut would move ``_compute_hash``
        without moving a single piece: a second cache entry for the identical
        plan. Coerced rather than rejected for the same reason as the rule above.
        """
        if self.pool_key is not None:
            self.skip_trim = False
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
    # Cantos especiales: per-side tapes that WIN over ``edge_banding`` on the
    # sides they name, and add the side when ``edge_banding`` did not band it.
    # The seller's input is kept as typed and the precedence is resolved when
    # the payload is built (``side_products``): normalizing ``edge_banding.sides``
    # instead would not survive the round trip, since the web writes the auto
    # sides as a count (``1L`` is always ``left``). Empty is left out of the
    # hash and of the cached payload, so no existing quote moves.
    special_edges: List[SpecialEdge] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Per-side edge banding overriding (or adding to) `edgeBanding` on "
            "the sides it names; at most one entry per side"
        ),
    )
    # The shop's own work on the piece, as the codes the workshop already knows.
    # Free text on purpose: the seller types what the bander reads, and this
    # system has no opinion on what a code means. They are production data, not
    # geometry and not billing (the billed services are a separate list), so
    # they stay OUT of the optimization hash and the cached payload -- see
    # ``WORKSHOP_CODE_FIELDS``. Any of them set is what makes the order carry
    # the ``additional`` activity.
    hinging_code: Optional[str] = Field(
        default=None, max_length=32, description="Abisagrado: workshop code"
    )
    grooving_code: Optional[str] = Field(
        default=None, max_length=32, description="Ranurado: workshop code"
    )
    assembly_code: Optional[str] = Field(
        default=None, max_length=32, description="Ensamble: workshop code"
    )
    division_code: Optional[str] = Field(
        default=None, max_length=32, description="División: workshop code"
    )

    @field_validator(
        "hinging_code", "grooving_code", "assembly_code", "division_code", mode="before"
    )
    @classmethod
    def _blank_code_is_none(cls, value):
        """One spelling of "no work": a blank code is ``None``, everywhere.

        Stripped before ``max_length`` runs, so padding never costs a
        character. It is what lets the order ask "does this piece carry work"
        with a plain ``IS NOT NULL``.
        """
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("special_edges")
    @classmethod
    def _one_special_edge_per_side(cls, edges: List[SpecialEdge]) -> List[SpecialEdge]:
        if len({e.side for e in edges}) != len(edges):
            raise ValueError("specialEdges must not repeat a side")
        return edges

    def side_products(self) -> Dict[str, Optional[int]]:
        """The tape each banded side actually gets: ``{side: product_id}``.

        The single definition of the precedence: a canto especial wins on its
        side, ``edge_banding`` keeps the rest of its sides (its ``product_id``
        may be ``None`` — geometry-only banding), and a special side the auto
        banding did not cover is added. Sides come in ``edge_banding.sides``
        order first, then the added ones: for a piece with no special edge that
        is exactly the order every length sum ran in before they existed.
        """
        special = {e.side.value: e.product_id for e in self.special_edges}
        sides: Dict[str, Optional[int]] = {}
        if self.edge_banding is not None:
            for side in self.edge_banding.sides:
                sides[side.value] = special.get(
                    side.value, self.edge_banding.product_id
                )
        for side, pid in special.items():
            sides.setdefault(side, pid)
        return sides

    def side_length(self, side: str) -> int:
        """Length of a nominal side: ``width`` for top/bottom, ``height`` else."""
        return self.width if side in ("top", "bottom") else self.height


# The workshop codes of a requirement, in the order the documents print them.
# The single list the hash, the cached payload, the order and the PDF read: a
# code is never in the hash (editing one must not re-run a search that costs
# seconds with the client at the counter) and never in the cached payload (a
# cache hit would hand back another request's codes). The order takes them
# from its own request instead.
WORKSHOP_CODE_FIELDS = (
    "hinging_code",
    "grooving_code",
    "assembly_code",
    "division_code",
)


def has_workshop_codes(requirement: dict) -> bool:
    """Whether a dumped requirement carries any workshop code."""
    return any(requirement.get(f) for f in WORKSHOP_CODE_FIELDS)


class AdjustedPiece(CamelModel):
    """Where the seller put one piece instance, by hand.

    Only the corner and the orientation: the size comes from the requirement the
    id names, so a hand adjustment can never smuggle in a piece of another size.
    """

    piece_id: str = Field(
        ...,
        min_length=1,
        max_length=160,
        description="Instance id, as the layouts name it (`Puerta#2`)",
    )
    x: float = Field(..., description="X of the piece's corner, mm")
    y: float = Field(..., description="Y of the piece's corner, mm")
    rotated: bool = Field(default=False, description="Placed turned 90 degrees")


class WholeOffcut(CamelModel):
    """Free space the seller wants cut out in one piece: a *retazo entero*.

    The cut tree has to free it intact. A wish about free space, not an
    obstacle: a piece placed on it later simply uses it.
    """

    x: float
    y: float
    width: float = Field(..., gt=0)
    height: float = Field(..., gt=0)


class AdjustedSheet(CamelModel):
    """One sheet of a hand-adjusted pool: which stock it is and what sits on it.

    No dimensions and no price: both are read off the materials resolved for
    the request, so the sheet is billed at today's price and a sheet type the
    catalog stopped offering is caught instead of re-created.
    """

    material_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="The pool's anchor, or one of its offcuts",
    )
    half_board: bool = Field(
        default=False, description="The anchor's half board rather than the whole one"
    )
    pieces: List[AdjustedPiece] = Field(default_factory=list, max_length=5000)
    whole_offcuts: List[WholeOffcut] = Field(default_factory=list, max_length=100)


class LayoutAdjustment(CamelModel):
    """The seller's own layout for a whole pool: it replaces the engine's.

    A pool is the material the requirements point at plus every offcut whose
    `poolKey` names it — the unit the engine optimizes on its own, and the only
    one pieces can move within. A piece of the pool that sits on no sheet is
    pending.
    """

    pool_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Key of the pool's anchor material",
    )
    sheets: List[AdjustedSheet] = Field(default_factory=list, max_length=1000)


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
            "and its hash do not depend on the client); only the documents and orders "
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
    layout_adjustments: Optional[List[LayoutAdjustment]] = Field(
        default=None,
        description=(
            "The seller's hand adjustments to the plan, one entry per pool. Each "
            "entry REPLACES the engine's sheets for that pool; the others are "
            "left as the engine made them. Applied after the cache, so it is not "
            "part of the optimization hash. On a read, a pool whose adjustment no "
            "longer holds (the cut list changed, a sheet type is gone, a piece "
            "would overlap) is dropped and reported in `layoutIssues`."
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


class _QuietFlags(CamelModel):
    """Leaves the listed flags out of the JSON while they are ``False``.

    The hand-adjustment markers (``adjusted``, ``kept_whole``) are new keys on
    shapes every quote has always returned. Emitting them only when they are
    true keeps the response of a plan nobody adjusted exactly what it was.
    """

    quiet_flags: ClassVar[Tuple[str, ...]] = ()

    @model_serializer(mode="wrap")
    def _drop_false_flags(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        for name in self.quiet_flags:
            for key in (name, to_camel(name)):
                if data.get(key) is False:
                    del data[key]
        return data


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
        description=(
            "True if this sheet is a half board: half the catalog sheet at "
            "cost/2 plus markup. Which side is halved depends on the material, "
            "so read the width/height above rather than assuming."
        ),
    )


class PlacedPiece(_QuietFlags):
    quiet_flags = ("adjusted",)

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
    adjusted: bool = Field(
        default=False,
        description="Moved by hand: not where the optimizer put it",
    )


class Remainder(_QuietFlags):
    quiet_flags = ("kept_whole",)

    x: float = Field(..., description="X position of the remainder")
    y: float = Field(..., description="Y position of the remainder")
    height: float = Field(..., description="Height of the remainder (alto)")
    width: float = Field(..., description="Width of the remainder (ancho)")
    kept_whole: bool = Field(
        default=False,
        description=(
            "An offcut the seller asked to cut out in one piece (a whole offcut, "
            "see `layoutAdjustments`)"
        ),
    )


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


class Layout(_QuietFlags):
    quiet_flags = ("adjusted",)

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
    adjusted: bool = Field(
        default=False,
        description="This sheet differs from what the optimizer produced",
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

    A catalog board is unlimited, so a quote anchored on one only lists a piece
    here when it is larger than the board's useful area. A job cut on the
    client's retazos can also run out of material. Either way the plan does not
    cut it, and no write accepts that: the quote is not saved or sent and the
    order is not minted (``UNPLACED_PIECES``) until the seller fixes the size,
    changes the material, adds retazos or drops the piece.
    """

    material_key: str
    label: Optional[str] = None
    height: float
    width: float
    quantity: int = Field(..., description="How many instances did not fit")
    material_name: Optional[str] = Field(
        default=None, description="The board or retazo the pieces were meant for"
    )
    usable_height: Optional[float] = Field(
        default=None, description="That material's useful height, trims off"
    )
    usable_width: Optional[float] = Field(
        default=None, description="That material's useful width, trims off"
    )
    reason: Optional[UnplacedReason] = Field(
        default=None,
        description=(
            "`larger_than_sheet`: bigger than the sheet itself; "
            "`larger_than_trimmed_sheet`: bigger than the useful area, but it "
            "would fit untrimmed (`skipTrim`); `out_of_stock`: it fits, but the "
            "finite retazos ran out; `pending`: it fits, but the hand adjustment "
            "of its pool left it on no sheet"
        ),
    )


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
        description="Money block: net subtotal, tax and total",
    )
    unplaced: List[UnplacedPiece] = Field(
        default_factory=list,
        description=(
            "Pieces that did not fit the available stock, grouped by size: a "
            "piece larger than its board, or a pool of finite offcuts that ran "
            "out. The plan returned is valid for everything else, but these are "
            "pieces it does NOT cut, so no quote or order can be saved with any "
            "(422 `UNPLACED_PIECES`)."
        ),
    )
    layout_issues: List["LayoutIssue"] = Field(
        default_factory=list,
        description=(
            "Hand adjustments that were not applied, and why. On a read the pool "
            "falls back to the optimizer's plan; the seller sees this list."
        ),
    )
    adjustment_summary: Optional["AdjustmentSummary"] = Field(
        default=None,
        description="What the hand adjustments changed against the optimizer's plan",
    )
    layout_adjustments: Optional[List[LayoutAdjustment]] = Field(
        default=None,
        description="The hand adjustments actually applied to this plan",
    )


class LayoutIssue(CamelModel):
    """Why (part of) a hand adjustment does not hold."""

    pool_key: str
    code: str = Field(..., description="Stable machine-readable reason")
    message: str = Field(..., description="The reason, for the seller, in Spanish")
    sheet_index: Optional[int] = Field(
        default=None, description="0-based index of the sheet within the pool's list"
    )
    piece_ids: List[str] = Field(default_factory=list)


class AdjustmentSummary(CamelModel):
    """The hand adjustments against the optimizer's plan, at the quote's prices."""

    moved_pieces: int = Field(
        ..., description="Pieces not where the optimizer put them"
    )
    boards_delta: int = Field(..., description="Change in boards the client buys")
    board_cost_delta: float = Field(..., description="Change in what the sheets cost")
    whole_offcuts: int = Field(
        default=0, description="Offcuts the seller asked to cut out in one piece"
    )


OptimizeResponse.model_rebuild()


class EditablePiece(CamelModel):
    """One piece instance of a pool, as the layout editor needs it."""

    piece_id: str
    label: str
    width: float
    height: float
    can_rotate: bool


class SheetBinInfo(CamelModel):
    """A kind of sheet a pool may be cut on."""

    material_key: str
    half_board: bool
    width: float
    height: float
    cost_per_unit: float
    label: str
    remaining: Optional[int] = Field(
        default=None,
        description="Units still unused (offcuts); null means unlimited",
    )


class EditableSheet(AdjustedSheet):
    """A working sheet plus the layout derived from it (null when empty)."""

    layout: Optional[Layout] = None


class EditablePool(CamelModel):
    """Everything the editor needs to rearrange one pool."""

    pool_key: str
    label: str
    bins: List[SheetBinInfo]
    pieces: List[EditablePiece]
    pending: List[str] = Field(
        default_factory=list, description="Instance ids that sit on no sheet"
    )
    sheets: List[EditableSheet]
    adjusted: bool = Field(
        default=False, description="The pool carries a hand adjustment"
    )
    finite: bool = Field(
        default=False,
        description=(
            "Only offcuts, no board to open: pieces may legitimately stay pending. "
            "Anywhere else a pending piece blocks applying the adjustment."
        ),
    )


class LayoutEvaluateResponse(OptimizeResponse):
    """The plan with a working adjustment applied, plus the editor's context."""

    pools: List[EditablePool] = Field(default_factory=list)


class PieceProbe(CamelModel):
    """Where can this piece go: on one sheet, or on which sheets at all."""

    kind: Literal["piece"]
    pool_key: str
    piece_id: str
    sheet_index: Optional[int] = Field(
        default=None,
        description="Target sheet; null asks which sheets can take the piece",
    )


class LeftoverProbe(CamelModel):
    """How far can this offcut grow and still be cut whole."""

    kind: Literal["leftover"]
    pool_key: str
    sheet_index: int
    x: float
    y: float
    width: float
    height: float


class SheetProbe(CamelModel):
    """Which sizes can this sheet take (whole board / half board)."""

    kind: Literal["sheet"]
    pool_key: str
    sheet_index: int


LayoutProbe = Annotated[
    Union[PieceProbe, LeftoverProbe, SheetProbe], Field(discriminator="kind")
]


class LayoutCandidatesRequest(OptimizeRequest):
    probe: LayoutProbe


class CandidatePosition(CamelModel):
    x: float
    y: float
    rotated: bool
    uses_whole_offcuts: List[int] = Field(
        default_factory=list,
        description=(
            "Whole offcuts of the sheet (indices into its `wholeOffcuts`) this "
            "position lands on: placing the piece there uses them, so they are no "
            "longer kept whole."
        ),
    )


class SheetFit(CamelModel):
    sheet_index: int
    fits: bool = Field(..., description="The piece fits as it is")
    fits_rotated: bool = Field(..., description="The piece fits turned 90 degrees")
    positions: List[CandidatePosition] = Field(default_factory=list)
    free_rects: List[Remainder] = Field(
        default_factory=list, description="Maximal free rectangles of the sheet"
    )


class LeftoverExtension(CamelModel):
    direction: Literal["x+", "x-", "y+", "y-"]
    x: float
    y: float
    width: float
    height: float


class SheetConversion(CamelModel):
    half_board: bool
    shift_x: float = 0.0
    shift_y: float = 0.0


class LayoutCandidatesResponse(CamelModel):
    sheets: List[SheetFit] = Field(default_factory=list)
    extensions: List[LeftoverExtension] = Field(default_factory=list)
    conversions: List[SheetConversion] = Field(default_factory=list)
