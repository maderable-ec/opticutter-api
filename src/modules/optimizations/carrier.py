from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class ProformaCarrier:
    """Duck-typed carrier that the proforma and production sheet know how to render.

    Unifies the two sources of the same computation — an ephemeral optimization
    (cached by hash) or an order's immutable snapshot — exposing the same
    attributes that ``ProformaService`` reads, without coupling the render to a
    concrete ORM model. The render only depends on this shape, not its origin.
    """

    reference: str
    client: object
    company: dict = field(default_factory=dict)
    # Validity (days) shown on the proforma; ``None`` omits it (e.g. an already
    # confirmed order isn't a current quote). Set by the quoting carriers.
    validity_days: Optional[int] = None
    # Free-form commercial reference (project/site name) typed by the seller: the
    # differentiator when the same client has several jobs running. ``None``/empty
    # omits the line from every document.
    notes: Optional[str] = None
    requirements: List[dict] = field(default_factory=list)
    materials_summary: List[dict] = field(default_factory=list)
    edge_bandings_summary: List[dict] = field(default_factory=list)
    layouts: List[dict] = field(default_factory=list)
    layout_groups: List[dict] = field(default_factory=list)
    total_boards_used: int = 0
    total_boards_cost: float = 0.0
    total_edge_banding_cost: float = 0.0
    total_cut_linear_m: float = 0.0
    total_edge_banding_linear_m: float = 0.0
    # The money, computed once by ``pricing.build_pricing`` and carried here
    # rather than recomputed: there used to be a second implementation of the
    # same arithmetic on this class, which is exactly the kind of duplication
    # that drifts. ``subtotal`` is net (boards + edge banding + services),
    # ``total`` is what the client pays.
    price_level_name: Optional[str] = None
    subtotal: float = 0.0
    services_total: float = 0.0
    tax_rate: float = 0.0
    tax_amount: float = 0.0
    total_cost: float = 0.0
    # Billed additional services (qty × TAX-INCLUDED unit price, as staff
    # registers them). The renderers divide by ``1 + tax_rate`` to print them
    # net like every other line. Empty for documents without services.
    additional_services: List[dict] = field(default_factory=list)
    # Dispatch data (only the dispatch sheet uses it; ``None`` omits it). Set by
    # ``from_order`` from the order; the ephemeral-optimization path doesn't.
    dispatch_date: Optional[datetime] = None
    dispatched_by_label: Optional[str] = None
    # Frozen payment method (informational). Only ``from_order`` sets it; ephemeral
    # quotes leave it as ``None`` and the block is omitted from the PDF.
    payment_cash_amount: Optional[float] = None
    payment_transfer_amount: Optional[float] = None
    payment_credit_amount: Optional[float] = None

    def service_net(self, service: dict) -> float:
        """One service line's net total (it is registered tax-included)."""
        gross = service.get("unit_price", 0.0) * service.get("quantity", 0)
        return round(gross / (1 + self.tax_rate), 2)

    @classmethod
    def from_payload(
        cls,
        payload: dict,
        client,
        reference: str,
        company: dict | None = None,
        validity_days: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> "ProformaCarrier":
        """Builds the carrier from an optimization payload + the client.

        ``company`` is the current letterhead (company data) rendered live,
        including the full configured branch list; it's not part of the priced
        snapshot. ``validity_days`` is the quote's validity period shown on the
        proforma (``None`` omits it). ``notes`` is the commercial reference: it
        lives on the pre-order/order row, not in the optimization payload, so the
        caller passes it in.
        """
        company = company or {}
        # Money block, attached by build_pricing before the carrier is assembled.
        pricing = payload.get("pricing") or {}
        return cls(
            reference=reference,
            client=client,
            company=company,
            validity_days=validity_days,
            notes=notes,
            requirements=payload.get("requirements") or [],
            materials_summary=payload.get("materials_summary") or [],
            edge_bandings_summary=payload.get("edge_bandings_summary") or [],
            layouts=payload.get("layouts") or [],
            layout_groups=payload.get("layout_groups") or [],
            total_boards_used=payload.get("total_boards_used", 0),
            total_boards_cost=payload.get("total_boards_cost", 0.0),
            total_edge_banding_cost=payload.get("total_edge_banding_cost", 0.0),
            total_cut_linear_m=payload.get("total_cut_linear_m", 0.0),
            total_edge_banding_linear_m=payload.get("total_edge_banding_linear_m", 0.0),
            price_level_name=pricing.get("price_level_name"),
            subtotal=pricing.get("subtotal", 0.0),
            services_total=pricing.get("services_total", 0.0),
            tax_rate=pricing.get("tax_rate", 0.0),
            tax_amount=pricing.get("tax_amount", 0.0),
            total_cost=pricing.get("total", 0.0),
            additional_services=payload.get("additional_services") or [],
        )

    @classmethod
    def from_order(cls, order, company: dict | None = None) -> "ProformaCarrier":
        """Builds the carrier from an order (snapshot + frozen prices).

        The breakdown (boards vs edge banding) is taken from the immutable
        snapshot; the frozen money (net subtotal, tax rate and amount, total)
        lives in the order's columns, so raising the tax rate in settings never
        rewrites a document already issued. The letterhead (``company``) is
        rendered live, not frozen into the snapshot, and always lists every
        configured branch (not scoped to the order's own branch).
        """
        snapshot = order.optimization_snapshot or {}
        reference = order.code or f"ORD-{order.id:06d}"
        carrier = cls.from_payload(
            snapshot,
            order.client,
            reference=reference,
            company=company,
            notes=order.notes,
        )
        # The order freezes the board count when confirmed.
        carrier.total_boards_used = order.total_boards_used
        # The frozen money lives in the order's columns (source of truth); the
        # level's name comes from the snapshot (already read by from_payload).
        carrier.subtotal = order.subtotal
        carrier.services_total = order.additional_services_total or 0.0
        carrier.tax_rate = order.tax_rate
        carrier.tax_amount = order.tax_amount
        carrier.total_cost = order.total
        # Frozen dispatch data (shown by the dispatch sheet; ``None`` before dispatch).
        carrier.dispatch_date = order.dispatched_at
        carrier.dispatched_by_label = order.dispatched_by_label
        # Frozen payment method (``None`` before moving to the queue).
        carrier.payment_cash_amount = order.payment_cash_amount
        carrier.payment_transfer_amount = order.payment_transfer_amount
        carrier.payment_credit_amount = order.payment_credit_amount
        return carrier
