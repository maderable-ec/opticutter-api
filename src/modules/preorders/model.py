from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class PreOrderStatus(str, Enum):
    """States of a pre-order (mutable quote prior to the order)."""

    draft = "draft"
    sent = "sent"
    changes_requested = "changes_requested"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"


# Open (editable): they count toward the per-client anti-abuse cap and the lazy
# expiry sweep. ``changes_requested`` is open: the client asked for an
# adjustment from the link and the ball is back with the workshop, which edits
# and resends it.
OPEN_STATUSES = {
    PreOrderStatus.draft,
    PreOrderStatus.sent,
    PreOrderStatus.changes_requested,
}

# No way out: the pre-order no longer transforms.
TERMINAL_STATUSES = {
    PreOrderStatus.confirmed,
    PreOrderStatus.rejected,
    PreOrderStatus.expired,
    PreOrderStatus.cancelled,
}


class ReviewLinkStatus(str, Enum):
    """States of a client review link."""

    active = "active"
    used = "used"
    revoked = "revoked"


class PreOrderModel(TimestampMixin, AuditMixin, Base):
    """Mutable quote: optimizer inputs + the client's review link.

    Unlike the Order (a frozen immutable snapshot), the pre-order only stores
    the **inputs** (``materials`` + ``requirements``, shaped like ``OptimizeRequest``)
    and recomputes the result on demand (cache-first): this lets it be edited
    freely with live prices until confirmed. Once the client approves it on the
    review link, the immutable Order is materialized and linked via ``order_id``.
    """

    __tablename__ = "preorders"
    __table_args__ = (
        # Lazy-expiry sweep filters open quotes by branch + status; also serves
        # branch-only listing via its leftmost column.
        Index("ix_preorders_branch_status", "branch_id", "status"),
        # Per-client open-quote anti-abuse cap counts by client + status.
        Index("ix_preorders_client_status", "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[Optional[str]] = mapped_column(String(32), unique=True, nullable=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    # Branch owning the quote (inherited by the order upon confirmation). Indexed
    # via the composite (branch_id, status) in __table_args__.
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"))
    status: Mapped[str] = mapped_column(String(32), default=PreOrderStatus.draft.value)

    # Optimizer inputs, as-is, for the recompute (no snapshot is stored).
    materials: Mapped[list] = mapped_column(JSON)
    requirements: Mapped[list] = mapped_column(JSON)

    # Billed additional services (not cut geometry): stored as-is and folded into
    # the total after the discount (see build_pricing). Empty for quotes without
    # services; server_default keeps pre-feature rows valid.
    additional_services: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    # Selected price tier (live discount; frozen once the order is confirmed).
    price_tier_code: Mapped[str] = mapped_column(
        String(32), default="consumidor", server_default="consumidor"
    )

    # Chosen packing heuristic (affects the recompute's geometry): kept so each
    # read reproduces the same result and the order inherits it upon
    # confirmation. See OptimizationStrategy (default | longOffcuts).
    strategy: Mapped[str] = mapped_column(
        String(32), default="default", server_default="default"
    )

    # Alternative-solution seed ("Generar otra alternativa"): like ``strategy``
    # it affects the recompute's geometry and hash, so it's kept for every read
    # to reproduce the chosen layout and inherited by the order on confirmation.
    variant: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Latest client change request (free text from the review link); cleared
    # once the workshop edits and resends the pre-order.
    client_note: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Immutable order created upon confirmation (null while the pre-order is open).
    # SET NULL so removing an order doesn't block on the back-reference.
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    client: Mapped["ClientModel"] = relationship("ClientModel")  # noqa: F821
    branch: Mapped["BranchModel"] = relationship("BranchModel")  # noqa: F821
    order: Mapped[Optional["OrderModel"]] = relationship(  # noqa: F821
        "OrderModel", foreign_keys=[order_id]
    )
    review_links: Mapped[list["PreOrderReviewLinkModel"]] = relationship(
        "PreOrderReviewLinkModel",
        back_populates="preorder",
        cascade="all, delete-orphan",
        order_by="PreOrderReviewLinkModel.id",
    )
    history: Mapped[list["PreOrderStatusHistoryModel"]] = relationship(
        "PreOrderStatusHistoryModel",
        back_populates="preorder",
        cascade="all, delete-orphan",
        order_by="PreOrderStatusHistoryModel.id",
    )


class PreOrderReviewLinkModel(TimestampMixin, AuditMixin, Base):
    """Secure client review link (the token is the credential).

    Only the token's sha256 is persisted; the raw token is returned exactly
    once at generation time and is unrecoverable (losing it means regenerating
    it, which revokes the previous one). A single ``active`` link per
    pre-order, enforced in the service.
    """

    __tablename__ = "preorder_review_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    preorder_id: Mapped[int] = mapped_column(
        ForeignKey("preorders.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(
        String(16), default=ReviewLinkStatus.active.value
    )
    # Mirrors preorder.expires_at at generation time (defense in depth).
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Audit trail of the client's action: {"action", "ip", "user_agent", "note"}.
    used_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    preorder: Mapped["PreOrderModel"] = relationship(
        "PreOrderModel", back_populates="review_links"
    )


class PreOrderStatusHistoryModel(TimestampMixin, AuditMixin, Base):
    """Audit trail of a pre-order's status transitions (mirrors orders).

    ``actor`` is the TYPE (``staff``/``client``/``system``); ``actor_user_id`` is
    the FK to the staff user (NULL for client/system) and ``actor_label`` is the
    readable name snapshot at the time of the event.
    """

    __tablename__ = "preorder_status_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    preorder_id: Mapped[int] = mapped_column(
        ForeignKey("preorders.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    preorder: Mapped["PreOrderModel"] = relationship(
        "PreOrderModel", back_populates="history"
    )
