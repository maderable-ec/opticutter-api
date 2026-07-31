from datetime import datetime
from typing import List, Optional

from pydantic import Field

from src.modules.branches.schemas import BranchRefResponse
from src.modules.clients.schemas import ClientResponse
from src.modules.optimizations.schemas import (
    AdditionalServiceLine,
    MaterialInput,
    OptimizationStrategy,
    OptimizeResponse,
    Remainder,
    Requirement,
)
from src.modules.preorders.model import PreOrderStatus, ReviewLinkStatus
from src.shared.schemas import CamelModel


class PreOrderCreate(CamelModel):
    """Create a pre-order (mutable quote): optimizer inputs + metadata.

    Same input shape as ``OptimizeRequest``/``OrderCreate`` (materials of any
    source + cut list), but nothing is frozen: it's recomputed on read.
    """

    materials: List[MaterialInput] = Field(
        ...,
        min_length=1,
        description="Available materials (stock): catalog boards, offcuts or manual",
    )
    requirements: List[Requirement] = Field(
        ..., min_length=1, description="Cut list to optimize"
    )
    additional_services: List[AdditionalServiceLine] = Field(
        default_factory=list,
        description="Billed additional services (qty × editable unit price)",
    )
    client_id: int = Field(..., description="Client the quote is for")
    price_tier_code: Optional[str] = Field(
        default="consumidor",
        max_length=32,
        description="Price tier: consumidor (0%) | carpintero (2%) | efectivo (5%)",
    )
    strategy: OptimizationStrategy = Field(
        default=OptimizationStrategy.default,
        description=(
            "Packing heuristic to remember for the recompute: default | longOffcuts. "
            "Affects geometry; inherited by the order upon confirmation."
        ),
    )
    variant: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Alternative-solution seed remembered for the recompute; like "
            "strategy it affects geometry and is inherited by the order."
        ),
    )
    notes: Optional[str] = Field(default=None, max_length=512)
    source: Optional[str] = Field(default="web", max_length=32)
    branch_id: Optional[int] = Field(
        default=None,
        description=(
            "Target branch. Ignored for the operator (forced to their own branch); "
            "optional for the seller (defaults to their base branch, overridable); "
            "required for a global admin."
        ),
    )


class PreOrderUpdate(CamelModel):
    """Edit an open pre-order (only while ``draft``/``sent``). Everything optional."""

    materials: Optional[List[MaterialInput]] = Field(default=None, min_length=1)
    requirements: Optional[List[Requirement]] = Field(default=None, min_length=1)
    additional_services: Optional[List[AdditionalServiceLine]] = Field(default=None)
    client_id: Optional[int] = None
    price_tier_code: Optional[str] = Field(default=None, max_length=32)
    strategy: Optional[OptimizationStrategy] = Field(default=None)
    variant: Optional[int] = Field(default=None, ge=0, le=1000)
    notes: Optional[str] = Field(default=None, max_length=512)
    source: Optional[str] = Field(default=None, max_length=32)


class PreOrderStatusHistoryResponse(CamelModel):
    """Audit entry for a pre-order status transition."""

    id: int
    from_status: Optional[PreOrderStatus] = None
    to_status: PreOrderStatus
    actor: Optional[str] = Field(
        default=None, description="Actor type: staff | client | system"
    )
    actor_user_id: Optional[int] = Field(
        default=None, description="Staff user id (null for client/system)"
    )
    actor_label: Optional[str] = Field(
        default=None, description="Frozen actor name at the time of the action"
    )
    note: Optional[str] = None
    created_at: datetime


class PreOrderResponse(CamelModel):
    """Pre-order detail with its recomputed optimization (live prices)."""

    id: int
    code: Optional[str] = None
    client: ClientResponse = Field(..., description="Client information")
    branch: BranchRefResponse = Field(..., description="Owning branch")
    status: PreOrderStatus
    price_tier_code: str = Field(
        default="consumidor", description="Selected price tier (discount level)"
    )
    strategy: OptimizationStrategy = Field(
        default=OptimizationStrategy.default,
        description="Packing heuristic remembered for the recompute",
    )
    variant: int = Field(
        default=0, description="Alternative-solution seed remembered for the recompute"
    )
    notes: Optional[str] = None
    client_note: Optional[str] = Field(
        default=None, description="Latest change request typed by the client"
    )
    source: Optional[str] = None
    order_id: Optional[int] = Field(
        default=None, description="Immutable order, set once the client confirms"
    )
    created_at: datetime
    updated_at: datetime
    sent_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Raw editable inputs (what the optimizer form re-renders).
    materials: List[MaterialInput] = Field(
        ..., description="Stored material inputs (editable)"
    )
    requirements: List[Requirement] = Field(
        ..., description="Stored cut list inputs (editable)"
    )
    additional_services: List[AdditionalServiceLine] = Field(
        default_factory=list, description="Stored additional services (editable)"
    )
    optimization: OptimizeResponse = Field(
        ..., description="Recomputed cutting result with live prices (incl. services)"
    )
    history: List[PreOrderStatusHistoryResponse] = Field(default_factory=list)


class PreOrderSummaryResponse(CamelModel):
    """Lightweight summary for the listing (without the full optimization)."""

    id: int
    code: Optional[str] = None
    client: ClientResponse
    branch: BranchRefResponse = Field(..., description="Owning branch")
    status: PreOrderStatus
    notes: Optional[str] = Field(
        default=None,
        description="Commercial reference (project/site), shown as the listing subtitle",
    )
    source: Optional[str] = None
    order_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Review link + public projections (consumed by the Maderable frontend)
# ---------------------------------------------------------------------------


class ReviewLinkResponse(CamelModel):
    """Freshly generated review link: the only response that exposes the token."""

    token: str = Field(..., description="Raw token, returned only at generation time")
    url: str = Field(..., description="Full review URL for the Maderable frontend")
    status: ReviewLinkStatus
    expires_at: Optional[datetime] = None
    created_at: datetime


class ReviewLinkInfoResponse(CamelModel):
    """Metadata of the current link, without the token (unrecoverable by design)."""

    status: ReviewLinkStatus
    created_at: datetime
    expires_at: Optional[datetime] = None
    used_at: Optional[datetime] = None


class ReviewActionRequest(CamelModel):
    """Client action on the quote (confirm/reject)."""

    note: Optional[str] = Field(default=None, max_length=512)


class ReviewLineResponse(CamelModel):
    """Billing line projected for the client's public review."""

    product_code: Optional[str] = None
    product_name: Optional[str] = None
    quantity: float
    unit_price: float
    line_total: float


class ReviewServiceResponse(CamelModel):
    """Additional-service line projected for the client's public review."""

    name: str
    quantity: int
    unit_price: float
    line_total: float


class ReviewPieceResponse(CamelModel):
    """Cut-list piece projected for the public review."""

    label: Optional[str] = None
    material_code: Optional[str] = None
    material_name: Optional[str] = None
    height: int
    width: int
    quantity: int
    edges: Optional[dict] = None


class ReviewPieceEdges(CamelModel):
    """Edge banding of a drawn piece, without the catalog identifiers.

    ``sides`` are the *geometric* sides of the piece as placed (post-rotation),
    so the diagram can paint the bands where they physically go.
    """

    sides: List[str] = Field(
        default_factory=list, description="Banded sides: top | bottom | left | right"
    )
    color: Optional[str] = None
    band_type: Optional[str] = Field(
        default=None, description="Canonical band type (Suave/Duro)"
    )
    notation: Optional[str] = Field(
        default=None, description="Workshop notation, e.g. '2L1C CS'"
    )


class ReviewPlacedPiece(CamelModel):
    """A piece as laid out on the sheet, for the client-facing diagram.

    ``piece_id`` is the human label the client themselves typed (optionally
    suffixed ``#N`` when the label has several physical instances), so the
    diagram can name the piece without any extra field.
    """

    piece_id: str
    x: float
    y: float
    width: float = Field(..., description="Width on the sheet (after rotation)")
    height: float = Field(..., description="Height on the sheet (after rotation)")
    rotated: bool
    original_width: float = Field(..., description="Requested width (before rotation)")
    original_height: float = Field(
        ..., description="Requested height (before rotation)"
    )
    edges: Optional[ReviewPieceEdges] = None


class ReviewSheet(CamelModel):
    """The physical board a pattern is cut from, named rather than keyed."""

    material_name: Optional[str] = Field(
        default=None, description="Display name of the board (never the internal key)"
    )
    width: float
    height: float
    thickness: float
    half_board: bool = False


class ReviewLayoutGroup(CamelModel):
    """One cutting pattern, plus how many sheets are cut that way.

    Deliberately omits the guillotine ``cuts`` (saw travel is production
    information) and the efficiency/waste statistics: the client is shown how
    their pieces are laid out, not how well the shop nested them.
    """

    count: int = Field(..., description="Number of sheets cut with this pattern")
    sheet_numbers: List[int] = Field(default_factory=list)
    sheet: ReviewSheet
    placed_pieces: List[ReviewPlacedPiece] = Field(default_factory=list)
    remainders: List[Remainder] = Field(
        default_factory=list, description="Leftover rectangles on the sheet"
    )
    pieces_count: int = Field(..., description="Pieces placed on one sheet")


class ReviewPreOrderResponse(CamelModel):
    """Sanitized public view of the pre-order: what the client sees on the link.

    Deliberately excludes internal identifiers (numeric id, client_id), the
    client's contact details, the raw inputs and internal commercial metadata.
    Prices are live (recomputed); the breakdown is built from the optimization.
    """

    reference: Optional[str] = Field(
        default=None, description="Pre-order code shown to the client (PRE-...)"
    )
    status: PreOrderStatus
    order_code: Optional[str] = Field(
        default=None, description="Resulting order code once the client confirms"
    )
    client_note: Optional[str] = Field(
        default=None, description="The client's own change request, echoed back"
    )
    notes: Optional[str] = Field(
        default=None,
        description="Commercial reference (project/site); the client also sees it "
        "on the proforma PDF",
    )
    client_name: Optional[str] = None
    currency: str
    subtotal: float = Field(..., description="Sum at list price (before the discount)")
    price_tier_name: Optional[str] = Field(
        default=None, description="Name of the applied price tier"
    )
    discount_rate: float = Field(
        default=0.0, description="Applied discount (0.02 = 2%)"
    )
    discount_amount: float = Field(default=0.0)
    services_total: float = Field(
        default=0.0, description="Sum of additional services (added after discount)"
    )
    total: float = Field(..., description="Subtotal minus discount plus services")
    total_boards_used: int
    total_pieces: int = Field(
        default=0, description="Total physical pieces across the cut list"
    )
    created_at: datetime
    sent_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    lines: List[ReviewLineResponse] = Field(default_factory=list)
    additional_services: List[ReviewServiceResponse] = Field(default_factory=list)
    pieces: List[ReviewPieceResponse] = Field(default_factory=list)
    layout_groups: List[ReviewLayoutGroup] = Field(
        default_factory=list,
        description="Cutting patterns of the live optimization, for the diagram",
    )
