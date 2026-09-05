from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.modules.users.enums import UserRole
from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class OrderStatus(str, Enum):
    """States of an order's CUTTING process.

    The client's pre-purchase review (mutable quote) lives in the pre-order; an
    order is born ``confirmed`` and from there only advances through production.
    ``queued`` is the workshop queue: the order is ready but cutting hasn't started
    yet (entering ``cutting`` marks the start of the cut; ``cut`` marks its end).

    EDGE BANDING runs on a parallel, independent track (``BandingStatus``): the
    bander can band pieces the operator releases without waiting for the whole
    cut to finish.
    """

    confirmed = "confirmed"
    queued = "queued"
    cutting = "cutting"
    cut = "cut"
    completed = "completed"
    dispatched = "despachado"
    cancelled = "cancelled"


class BandingStatus(str, Enum):
    """Status of the parallel EDGE BANDING track.

    Orthogonal dimension to ``OrderStatus``: it advances on its own while
    cutting follows its course. ``not_applicable`` = the order has no edge
    banding (nothing to band). The bander moves it ``pending → in_progress →
    done``.
    """

    not_applicable = "not_applicable"
    pending = "pending"
    in_progress = "in_progress"
    done = "done"


# Banding statuses that still block closing the order (banding work remains).
BANDING_PENDING_STATUSES = {BandingStatus.pending, BandingStatus.in_progress}

# Cutting statuses in which banding can be registered (pieces are already released).
BANDING_MUTABLE_ORDER_STATUSES = {OrderStatus.cutting, OrderStatus.cut}

# Statuses shown on the shared workshop board (operator + bander): from the queue
# up to "cut" (ready to complete). Excludes ``confirmed`` (not yet in the shop) and
# the closed states (``completed``/``despachado``/``cancelled``).
WORKSHOP_QUEUE_STATUSES = {OrderStatus.queued, OrderStatus.cutting, OrderStatus.cut}

# Statuses with no outgoing transition: the order no longer changes.
# ``dispatched`` (goods handed to the client) is the real end of the cycle;
# ``completed`` is no longer terminal in the graph (it advances to
# ``dispatched``) but still counts as "not active" for duplicate
# detection/pending-order cap purposes.
TERMINAL_STATUSES = {
    OrderStatus.completed,
    OrderStatus.dispatched,
    OrderStatus.cancelled,
}

# Map of valid state-machine transitions.
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.confirmed: {OrderStatus.queued, OrderStatus.cancelled},
    OrderStatus.queued: {OrderStatus.cutting},
    OrderStatus.cutting: {OrderStatus.cut, OrderStatus.queued},
    OrderStatus.cut: {OrderStatus.completed},
    OrderStatus.completed: {OrderStatus.dispatched},
    OrderStatus.dispatched: set(),
    OrderStatus.cancelled: set(),
}

# Which roles can execute each transition (from, to) → allowed roles.
TRANSITION_ROLES: dict[tuple[OrderStatus, OrderStatus], tuple[UserRole, ...]] = {
    (OrderStatus.confirmed, OrderStatus.queued): (
        UserRole.ADMIN,
        UserRole.SELLER,
    ),
    (OrderStatus.confirmed, OrderStatus.cancelled): (UserRole.ADMIN, UserRole.SELLER),
    (OrderStatus.queued, OrderStatus.cutting): (
        UserRole.ADMIN,
        UserRole.OPERATOR,
    ),
    (OrderStatus.cutting, OrderStatus.queued): (UserRole.ADMIN,),
    (OrderStatus.cutting, OrderStatus.cut): (UserRole.ADMIN, UserRole.OPERATOR),
    # Completing the order can be done by the shop floor too: the operator (own
    # cutting) or the bander (after finishing the banding). Gate B still blocks
    # completion while banding is pending/in_progress, so an operator can't close
    # an order the bander is still working on.
    (OrderStatus.cut, OrderStatus.completed): (
        UserRole.ADMIN,
        UserRole.SELLER,
        UserRole.OPERATOR,
        UserRole.BANDER,
    ),
    # Dispatch (physical handover to the client) is a commercial act: only
    # admin/seller register it, never the shop floor (operator/bander).
    (OrderStatus.completed, OrderStatus.dispatched): (
        UserRole.ADMIN,
        UserRole.SELLER,
    ),
}

# Valid transitions of the banding track (forward-only; re-applying is idempotent).
BANDING_TRANSITIONS: dict[BandingStatus, set[BandingStatus]] = {
    BandingStatus.pending: {BandingStatus.in_progress},
    BandingStatus.in_progress: {BandingStatus.done},
    BandingStatus.done: set(),
}

# Which roles can move the banding track.
BANDING_TRANSITION_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN, UserRole.BANDER)


class OrderModel(TimestampMixin, AuditMixin, Base):
    """Aggregate root: order with an immutable snapshot and a state machine."""

    __tablename__ = "orders"
    __table_args__ = (
        # The workshop board and every status check filter by branch + status;
        # the composite also serves branch-only filters via its leftmost column
        # (so no separate branch_id index is needed).
        Index("ix_orders_branch_status", "branch_id", "status"),
        # A client's order list / pending-cap / duplicate-detection queries.
        Index("ix_orders_client_id", "client_id"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint(
            "additional_services_total >= 0",
            name="additional_services_total_non_negative",
        ),
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 1", name="tax_rate_ratio"),
        CheckConstraint("price_level BETWEEN 1 AND 3", name="price_level_in_range"),
        CheckConstraint("payment_cash_amount >= 0", name="payment_cash_non_negative"),
        CheckConstraint(
            "payment_transfer_amount >= 0", name="payment_transfer_non_negative"
        ),
        CheckConstraint(
            "payment_credit_amount >= 0", name="payment_credit_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[Optional[str]] = mapped_column(String(32), unique=True, nullable=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    # Branch that owns the order: inherited from the pre-order on confirmation.
    # Reassigning a seller's branch doesn't move their past orders. Admin/seller
    # can reassign it (load rebalancing) while the order is 'confirmed'/'queued'
    # via change_branch(); frozen once the shop floor starts. Indexed via the
    # composite (branch_id, status) in __table_args__.
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"))
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.confirmed.value)

    optimization_snapshot: Mapped[dict] = mapped_column(JSON)
    optimization_hash: Mapped[str] = mapped_column(String(64))

    currency: Mapped[str] = mapped_column(String(8), default="USD")
    # subtotal = NET sum of everything the document prints (boards + edge
    # banding + services); total = subtotal + tax_amount. The catalog's price
    # level chosen when quoting is already baked into the line prices, so it is
    # frozen here only for the record. The tax rate is frozen with it: the rate
    # changes by law, and a document already issued must keep the one it was
    # billed at.
    subtotal: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)
    price_level: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # Frozen NET sum of additional services (registered tax-included, stored net
    # so it adds up to ``subtotal``). The per-line breakdown lives in
    # ``optimization_snapshot["additional_services"]``.
    additional_services_total: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0"
    )
    total_boards_used: Mapped[int] = mapped_column(Integer)

    external_invoice_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Priority attention: breaks the workshop board's FIFO on business request (an
    # urgent client). Purely a queue-ordering concern -- it touches neither the
    # status machine nor a single price. Named ``is_priority`` and not ``priority``
    # because ``OrderPieceModel.priority`` already means the optimizer's cutting
    # priority, which is a different thing entirely.
    is_priority: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # When the order actually entered the production queue (``confirmed -> queued``),
    # which is gated on registering the payment. This -- not ``created_at`` -- is the
    # workshop's arrival time: a quote raised on Monday and paid on Friday reaches the
    # shop AFTER one raised on Wednesday and paid on Thursday, so ordering the board by
    # creation jumps the line for whoever asked first rather than whoever paid first.
    # Frozen once, on the FIRST entry: the admin rollback ``cutting -> queued`` undoes
    # somebody taking the wrong order, and must not cost the client their place.
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Self-assigned operator: filled in when transitioning to ``cutting``.
    assigned_to_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    assigned_to_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Dispatch (physical handover to the client): frozen when transitioning to
    # ``dispatched``. The dispatch sheet shows this date and who handed it over.
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dispatched_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dispatched_by_label: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )

    # Payment method (informational only): captured when transitioning from
    # ``confirmed`` to ``queued``. An order can be paid with several methods at
    # once; the method used is inferred from which amount is > 0. Doesn't affect
    # pricing or billing.
    payment_cash_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payment_transfer_amount: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    payment_credit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # EDGE BANDING track (parallel to cutting): the bander marks start/finish. Set
    # to ``pending`` on creation if the order has edge banding, else ``not_applicable``.
    banding_status: Mapped[str] = mapped_column(
        String(16),
        default=BandingStatus.not_applicable.value,
        server_default=BandingStatus.not_applicable.value,
    )
    banding_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    banding_started_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    banding_started_by_label: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    banding_finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    banding_finished_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    banding_finished_by_label: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )

    client: Mapped["ClientModel"] = relationship("ClientModel")  # noqa: F821
    branch: Mapped["BranchModel"] = relationship("BranchModel")  # noqa: F821
    lines: Mapped[list["OrderLineModel"]] = relationship(
        "OrderLineModel", back_populates="order", cascade="all, delete-orphan"
    )
    pieces: Mapped[list["OrderPieceModel"]] = relationship(
        "OrderPieceModel", back_populates="order", cascade="all, delete-orphan"
    )
    history: Mapped[list["OrderStatusHistoryModel"]] = relationship(
        "OrderStatusHistoryModel",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderStatusHistoryModel.id",
    )
    boards: Mapped[list["OrderBoardModel"]] = relationship(
        "OrderBoardModel",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderBoardModel.id",
    )
    attachments: Mapped[list["OrderAttachmentModel"]] = relationship(
        "OrderAttachmentModel",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderAttachmentModel.id",
    )

    @property
    def additional_services(self) -> list:
        """Frozen additional-service lines, read from the immutable snapshot.

        The per-line breakdown lives in the snapshot (not a table); the rolled-up
        ``additional_services_total`` is a column. Empty for pre-feature orders.
        """
        return (self.optimization_snapshot or {}).get("additional_services") or []


class OrderLineModel(TimestampMixin, AuditMixin, Base):
    """BILLING line: a charged product (quantity × frozen price).

    Today billing is by boards used; the model supports any product (board,
    edge banding, hardware) for future mixed orders.

    ``product_id`` is null for materials outside the catalog (offcuts or
    manual measurements): those are identified by ``product_code``/``product_name``.
    """

    __tablename__ = "order_lines"
    __table_args__ = (
        Index("ix_order_lines_order_id", "order_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_snapshot >= 0", name="unit_price_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    # 64, like ``order_boards.product_code``: a non-catalog line is identified by
    # the optimization's material ``key``, which the contract allows up to 64.
    product_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Boards: whole units. Edge banding: net linear meters + waste factor, billed
    # exactly (no rounding up to a whole meter) — same value as ``linear_m``.
    quantity: Mapped[float] = mapped_column(Float)
    unit_price_snapshot: Mapped[float] = mapped_column(Float)
    line_total: Mapped[float] = mapped_column(Float)
    avg_efficiency: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_area_m2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Edge banding: linear meters incl. waste (mirrors ``quantity``). Null for boards.
    linear_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Half board: the line was charged at half (width/2, cost/2). False for
    # full boards and edge banding.
    half_board: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    order: Mapped["OrderModel"] = relationship("OrderModel", back_populates="lines")


class OrderPieceModel(TimestampMixin, AuditMixin, Base):
    """Piece of the CUT LIST (production input; not billed).

    ``product_id`` references the board (``board``-type product) it's cut
    from; it's null when the material is outside the catalog (offcut or
    manual measurement).
    """

    __tablename__ = "order_pieces"
    __table_args__ = (
        Index("ix_order_pieces_order_id", "order_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    height: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    can_rotate: Mapped[bool] = mapped_column(Boolean, default=True)
    # Piece edge banding (nominal sides + product), e.g.
    # ``{"product_id": 42, "sides": ["top", "left"]}``. Null if not banded.
    edges: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    order: Mapped["OrderModel"] = relationship("OrderModel", back_populates="pieces")


class OrderBoardModel(TimestampMixin, AuditMixin, Base):
    """PHYSICAL board of the cutting plan, materialized from the snapshot.

    Each row is a real sheet to cut (the snapshot's ``layout_groups`` only
    deduplicate the view). ``sheet_number`` is the global 1..N sequence
    within the order (the snapshot's ``sheet_number`` resets per material).
    ``product_id`` is null for materials outside the catalog (offcut/manual).
    """

    __tablename__ = "order_boards"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    sheet_number: Mapped[int] = mapped_column(Integer)
    material_key: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    product_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    thickness: Mapped[float] = mapped_column(Float)
    # Physical half board: the operator cuts/uses a half (width/2). ``width``
    # already arrives split; this flag makes it explicit for the workshop view.
    half_board: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # Leftover rectangles from the snapshot (display + future offcut inventory).
    remainders: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Guillotine saw cuts, used to draw the cut lines. Null in orders whose
    # snapshot predates the serialization of ``cuts``.
    cuts: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    order: Mapped["OrderModel"] = relationship("OrderModel", back_populates="boards")
    pieces: Mapped[list["OrderPlacedPieceModel"]] = relationship(
        "OrderPlacedPieceModel",
        back_populates="board",
        cascade="all, delete-orphan",
        order_by="OrderPlacedPieceModel.id",
    )


class OrderPlacedPieceModel(TimestampMixin, AuditMixin, Base):
    """Piece PLACED on a physical board: the unit the operator marks.

    Geometry is already rotated (x, y, width, height) and ready to draw; the
    nominal dims (``original_*``) are used to group identical pieces on the
    frontend. ``piece_id`` preserves the snapshot's instance identity
    (``label#N``). ``cut_at`` null = pending cut; with a date = cut.
    """

    __tablename__ = "order_placed_pieces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    board_id: Mapped[int] = mapped_column(
        ForeignKey("order_boards.id", ondelete="CASCADE"), index=True
    )
    piece_id: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(String(128))
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    original_width: Mapped[float] = mapped_column(Float)
    original_height: Mapped[float] = mapped_column(Float)
    rotated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Geometric banded sides, as-is from the snapshot (null if not banded).
    edges: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cut_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Who marked the piece as cut: FK to the operator + frozen label.
    # NULL while pending (in sync with ``cut_at``).
    cut_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cut_by_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    order: Mapped["OrderModel"] = relationship("OrderModel")
    board: Mapped["OrderBoardModel"] = relationship(
        "OrderBoardModel", back_populates="pieces"
    )


class OrderAttachmentModel(TimestampMixin, AuditMixin, Base):
    """File annex (anexo) attached to an order: a PDF or a screenshot.

    Only metadata lives here; the bytes are on local disk under
    ``config.ATTACHMENTS_DIR`` at ``stored_key`` (``{order_id}/{uuid}.{ext}``).
    Attachments can only be added/removed while the order isn't in a terminal
    state (not completed/dispatched/cancelled).
    """

    __tablename__ = "order_attachments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    # Original client-supplied file name (display only; never used as a path).
    filename: Mapped[str] = mapped_column(String(255))
    # Relative on-disk key, unique per file: ``{order_id}/{uuid4}.{ext}``.
    stored_key: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)

    order: Mapped["OrderModel"] = relationship(
        "OrderModel", back_populates="attachments"
    )


class OrderStatusHistoryModel(TimestampMixin, AuditMixin, Base):
    """Audit trail of an order's state transitions.

    ``actor`` is the actor TYPE (``staff``/``client``/``system``);
    ``actor_user_id`` is the FK to the staff user (NULL for client/system) and
    ``actor_label`` is the human-readable name snapshot at the time of the event.
    """

    __tablename__ = "order_status_history"
    __table_args__ = (Index("ix_order_status_history_order_id", "order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    order: Mapped["OrderModel"] = relationship("OrderModel", back_populates="history")
