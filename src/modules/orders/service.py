from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from src.modules.branches.model import BranchModel
from src.modules.branches.service import resolve_branch_for_create
from src.modules.clients.model import ClientModel
from src.modules.clients.schemas import ClientResponse
from src.modules.clients.service import require_phone
from src.modules.inventory.service import StockService
from src.modules.notifications.emitter import (
    notify_order_branch_changed,
    notify_order_low_stock,
    notify_order_transition,
)
from src.modules.optimizations.patterns import base_label, piece_instance_ids
from src.modules.optimizations.pricing import build_pricing
from src.modules.optimizations.schemas import (
    WORKSHOP_CODE_FIELDS,
    OptimizeRequest,
    Requirement,
    has_workshop_codes,
)
from src.modules.optimizations.service import OptimizationService
from src.modules.orders.model import (
    ACTIVITY_FINISH_NEEDS_EVERY_PIECE,
    ACTIVITY_LABELS,
    ACTIVITY_MUTABLE_ORDER_STATUSES,
    ACTIVITY_PIECES,
    ACTIVITY_ROLES,
    ACTIVITY_START_NEEDS_A_CUT_PIECE,
    ACTIVITY_TRANSITIONS,
    TERMINAL_STATUSES,
    TRANSITION_ROLES,
    TRANSITIONS,
    WORKSHOP_QUEUE_STATUSES,
    ActivityStatus,
    ActivityType,
    OrderActivityModel,
    OrderBoardModel,
    OrderLineModel,
    OrderModel,
    OrderPieceModel,
    OrderPlacedPieceModel,
    OrderStatus,
    OrderStatusHistoryModel,
)
from src.modules.orders.schemas import (
    ActivityResult,
    CuttingPlanResponse,
    CuttingProgress,
    OrderActivityResponse,
    OrderBoardResponse,
    OrderCreate,
    OrderExportLine,
    OrderExportResponse,
    OrderPaymentInput,
    PieceCutResponse,
    PlacedPieceResponse,
    WorkshopQueueItem,
)

# Model only, never the pre-order service: ``preorders`` imports ``orders`` and
# not the other way round, and ``preorders/model.py`` imports neither -- which is
# what keeps this from being a cycle.
from src.modules.preorders.model import PreOrderModel
from src.modules.settings.service import SettingsService
from src.shared.audit import Actor, system_actor
from src.shared.branch_scope import BranchScopedMixin
from src.shared.database import get_db
from src.shared.exceptions import (
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    EntityNotFoundError,
    ValidationError,
)


def _has_payment(payment: Optional[OrderPaymentInput]) -> bool:
    """True if at least one amount (> 0) was registered in any payment method."""
    if payment is None:
        return False
    return any(
        (amount or 0) > 0
        for amount in (
            payment.cash_amount,
            payment.transfer_amount,
            payment.credit_amount,
        )
    )


def _piece_edges(requirement: dict) -> Optional[dict]:
    """What ``order_pieces.edges`` freezes for a requirement of the snapshot.

    The auto banding as it always was, plus ``special_edges`` when the piece
    has a canto especial. A piece banded ONLY with special edges still gets the
    auto keys, empty, so every row keeps one shape. ``None`` when unbanded.
    """
    auto = requirement.get("edge_banding")
    special = requirement.get("special_edges")
    if not special:
        return auto
    base = auto or {"sides": [], "product_id": None, "band_type": None, "alias": None}
    return {**base, "special_edges": special}


class OrderService(BranchScopedMixin):
    """Creates orders (immutable snapshot), manages states and anti-abuse.

    Branch-isolated (``BranchScopedMixin``): listings and by-id access are
    filtered by the user's branch; the admin (scope ``None``) sees all.
    """

    model = OrderModel

    def __init__(self, db: Session):
        self.db = db
        self.optimization_service = OptimizationService(db)
        self.settings_service = SettingsService(db)

    def get_or_404(self, order_id: int) -> OrderModel:
        order = self.db.get(OrderModel, order_id)
        if order is None:
            raise EntityNotFoundError("Order", order_id)
        return order

    def list_orders(
        self,
        status: Optional[List[OrderStatus]] = None,
        branch_scope: Optional[int] = None,
        branch_filter: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
        client_filter: Optional[int] = None,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
        sort: str = "oldest",
        is_priority: Optional[bool] = None,
        activity: Optional[ActivityType] = None,
        activity_status: Optional[ActivityStatus] = None,
    ) -> Tuple[List[OrderModel], int]:
        """Lists orders with total count: ``(items, total)``.

        ``status`` filters by one or more statuses (empty list/``None`` = all).
        ``branch_scope`` isolates staff to their branch; the admin (``None``)
        sees all and can narrow to one with ``branch_filter``.

        ``search`` matches the order code or the client (identifier/first/last
        name), and also the order id when the term is all digits -- the shop
        says "order 42" as often as it reads the code off the sheet.

        ``sort`` defaults to ``oldest`` because the workshop reads this listing
        FIFO; the back office asks for ``recent`` explicitly. ``is_priority``
        narrows to (or excludes) the prioritized orders but does NOT reorder
        them: floating them to the top is the shop-floor board's rule, and the
        back office's listing answers "which ones are marked", not "what next".

        ``sort="stalest"`` puts whatever stopped moving first: closed orders last,
        then oldest ``status_changed_at`` first. It is the office's control view --
        unlike ``is_priority``, this one DOES reorder, because "what has been
        sitting longest" is a question only an ordering can answer.

        ``activity``/``activity_status`` narrow to one stage of one parallel
        activity ("show me everything still to band"). Either alone works: the
        type alone means "orders that carry this activity at all".
        """
        # ``OrderResponse`` embeds client, branch, lines, pieces and history, so
        # without this every row of the page fires five lazy loads -- and the page
        # now refreshes itself to keep its clocks live.
        query = self.db.query(OrderModel).options(
            joinedload(OrderModel.client),
            joinedload(OrderModel.branch),
            selectinload(OrderModel.lines),
            selectinload(OrderModel.pieces),
            selectinload(OrderModel.history),
            selectinload(OrderModel.activities),
            # ``load_only`` is load-bearing, not tidiness: a pre-order carries the
            # whole cut list as JSON (``materials``/``requirements``), so without
            # it a page of 20 orders drags 20 full cut lists into memory to read
            # one code.
            selectinload(OrderModel.preorders).load_only(
                PreOrderModel.id, PreOrderModel.code
            ),
        )
        if status:
            query = query.filter(OrderModel.status.in_([s.value for s in status]))
        if client_filter is not None:
            query = query.filter(OrderModel.client_id == client_filter)
        if is_priority is not None:
            query = query.filter(OrderModel.is_priority.is_(is_priority))
        if activity is not None or activity_status is not None:
            # EXISTS rather than a join: an order has at most one row per type,
            # but a join would still duplicate rows if the filter is loosened
            # later, and the count above has to stay honest.
            exists = self.db.query(OrderActivityModel.id).filter(
                OrderActivityModel.order_id == OrderModel.id
            )
            if activity is not None:
                exists = exists.filter(OrderActivityModel.type == activity.value)
            if activity_status is not None:
                exists = exists.filter(
                    OrderActivityModel.status == activity_status.value
                )
            query = query.filter(exists.exists())
        if search:
            pattern = f"%{search}%"
            # Outer join: an order always has a client, but the join must not
            # silently drop rows if that ever stops holding.
            query = query.outerjoin(ClientModel, OrderModel.client_id == ClientModel.id)
            term = (
                OrderModel.code.ilike(pattern)
                | ClientModel.identifier.ilike(pattern)
                | ClientModel.first_name.ilike(pattern)
                | ClientModel.last_name.ilike(pattern)
            )
            if search.strip().isdigit():
                term = term | (OrderModel.id == int(search.strip()))
            query = query.filter(term)
        # ``created_at`` is UTC-naive (TimestampMixin), so the day boundaries are
        # UTC ones. ``created_to`` is inclusive: compare against the next midnight.
        if created_from is not None:
            query = query.filter(
                OrderModel.created_at >= datetime.combine(created_from, time.min)
            )
        if created_to is not None:
            query = query.filter(
                OrderModel.created_at
                < datetime.combine(created_to + timedelta(days=1), time.min)
            )
        query = self._apply_branch_scope(query, branch_scope, branch_filter)
        total = query.count()
        if sort == "stalest":
            # Closed orders last: their clock is deliberately mute (nothing is
            # going to move them), so floating them would bury the live ones.
            # ``TERMINAL_STATUSES`` is the same set the UI silences.
            closed = case(
                (OrderModel.status.in_([s.value for s in TERMINAL_STATUSES]), 1),
                else_=0,
            )
            query = query.order_by(
                closed.asc(),
                func.coalesce(
                    OrderModel.status_changed_at, OrderModel.created_at
                ).asc(),
                OrderModel.id.asc(),
            )
        else:
            query = query.order_by(
                OrderModel.id.desc() if sort == "recent" else OrderModel.id.asc()
            )
        orders = query.offset(offset).limit(limit).all()
        return orders, total

    def create(self, data: OrderCreate, actor: Optional[Actor] = None) -> OrderModel:
        """Recomputes (cache-first), freezes the snapshot and creates the order.

        Idempotent: an identical re-POST returns the existing active order.
        ``actor`` audits the origin (client on pre-order confirmation, or system).
        """
        actor = actor or system_actor()
        # Business rule: the client must exist and have a phone number on file
        # before any order is frozen (this also blocks a re-POST without a phone).
        client = self.db.get(ClientModel, data.client_id)
        if client is None:
            raise EntityNotFoundError("Client", data.client_id)
        require_phone(client)
        # The branch comes from a trusted caller (the pre-order on confirmation);
        # it's validated to exist and be active. ``branch_scope=None`` ⇒ requires branchId.
        branch_id = resolve_branch_for_create(self.db, None, data.branch_id)

        opt_request = OptimizeRequest(
            materials=data.materials,
            requirements=data.requirements,
            client_id=data.client_id,
            # Carried so ``compute`` re-prices the marked boards at this level
            # before the snapshot is frozen; it does not touch the hash.
            price_level=data.price_level,
            variant=data.variant,
        )
        payload, optimization_hash = self.optimization_service.compute(opt_request)

        # Orders accept materials outside the catalog (offcuts/manual): they're
        # frozen as-is from the snapshot. Their lines/pieces end up with a null
        # ``product_id`` and are identified by ``product_code``/``product_name``.

        # Additional services (billed on top, tax-included as staff registers
        # them). Not cut geometry: they aren't in the hash, so they factor into
        # dedupe (two identical cuts differing only in services aren't the same
        # order).
        additional_services = [
            s.model_dump(mode="json") for s in data.additional_services
        ]
        # The tax rate is read now and frozen with the order: raising it later
        # must never rewrite a document already issued.
        pricing = build_pricing(
            payload,
            data.price_level,
            additional_services,
            self.settings_service.get_tax_rate(),
        )
        # Everything commercial happens outside the hash — the price level, the
        # per-board marks, `wholeBoard`, the services — and every one of them
        # lands in ``subtotal``, so that single number is what tells two
        # otherwise-identical cut plans apart. ``total`` joins it to catch a
        # change in the tax rate, which moves nothing else.
        existing = self._find_active_duplicate(
            branch_id,
            data.client_id,
            optimization_hash,
            pricing["services_total"],
            pricing["subtotal"],
            pricing["total"],
        )
        if existing is not None:
            return existing

        # Lines are frozen at the level's prices (``compute`` already applied
        # them); the snapshot embeds `pricing` and the service breakdown so it's
        # self-contained.
        snapshot = {
            **payload,
            # The workshop codes are the one thing the order must take from its
            # own request and not from the payload: they are outside the hash,
            # so the payload never carries them (see ``WORKSHOP_CODE_FIELDS``).
            "requirements": _with_workshop_codes(
                payload["requirements"], data.requirements
            ),
            "pricing": pricing,
            "additional_services": additional_services,
        }

        # The order is born 'confirmed' (the client's prior review, formerly
        # 'quoted', now lives in the pre-order, which mints this order on confirmation).
        now = datetime.utcnow()
        order = OrderModel(
            client_id=data.client_id,
            branch_id=branch_id,
            status=data.status.value,
            optimization_snapshot=snapshot,
            optimization_hash=optimization_hash,
            currency="USD",
            subtotal=pricing["subtotal"],
            total=pricing["total"],
            price_level=pricing["price_level"],
            tax_rate=pricing["tax_rate"],
            tax_amount=pricing["tax_amount"],
            additional_services_total=pricing["services_total"],
            total_boards_used=payload["total_boards_used"],
            source=data.source,
            notes=data.notes,
            created_at=now,
            confirmed_at=now,
            status_changed_at=now,
            created_by=actor.user_id,
        )
        # Billing lines = boards used + edge banding (consumed products).
        order.lines = [
            OrderLineModel(
                product_id=m["product_id"],
                product_code=m.get("product_code"),
                product_name=m.get("product_name"),
                quantity=m["count"],
                unit_price_snapshot=m["cost_per_unit"],
                line_total=m["total_cost"],
                avg_efficiency=m.get("avg_efficiency"),
                total_area_m2=m.get("total_area_m2"),
                half_board=m.get("half_board", False),
            )
            for m in payload["materials_summary"]
        ] + [
            OrderLineModel(
                product_id=e["product_id"],
                product_code=e.get("product_code"),
                product_name=e.get("product_name"),
                quantity=e["billed_linear_m"],
                unit_price_snapshot=e["price_per_m"],
                line_total=e["total_cost"],
                linear_m=e.get("linear_m"),
            )
            for e in payload.get("edge_bandings_summary", [])
        ]
        # Cut list = pieces (production input; not billed). The product is
        # resolved by the material's key (null if the material isn't from the catalog),
        # and the key itself is frozen alongside it: it is the only identity that
        # survives a client's offcut, a manual measurement, or two pools of the same
        # board. ``_dump_requirement`` puts all three on the requirement.
        product_id_by_key = {
            m["material_key"]: m["product_id"] for m in payload.get("materials", [])
        }
        order.pieces = [
            OrderPieceModel(
                material_key=r["material_key"],
                product_id=product_id_by_key[r["material_key"]],
                product_code=r.get("product_code"),
                product_name=r.get("product_name"),
                label=r.get("label"),
                height=r["height"],
                width=r["width"],
                quantity=r["quantity"],
                priority=r.get("priority", 0),
                can_rotate=r.get("can_rotate", True),
                edges=_piece_edges(r),
                **{f: r.get(f) for f in WORKSHOP_CODE_FIELDS},
            )
            for r in snapshot["requirements"]
        ]
        # Cutting plan = physical boards with each placed piece (the unit the
        # operator marks in the workshop; mutable state outside the snapshot).
        # The anchors are read off the request because the payload does not
        # keep ``pool_key``, and a piece cut on a pooled retazo is named after
        # its anchor's cut list.
        _attach_cutting_plan(
            order,
            snapshot,
            anchor_of={
                m.key: getattr(m, "pool_key", None) or m.key for m in data.materials
            },
        )
        # The work of ``in_process``, one row per APPLICABLE activity: the cut
        # always, the banding when something is billed as edge banding, the
        # additional work when a piece carries a workshop code. A missing row is
        # the old ``not_applicable`` -- it cannot be started and never holds the
        # closing gate.
        order.activities = _build_activities(snapshot)
        order.history = [
            OrderStatusHistoryModel(
                from_status=None,
                to_status=data.status.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note="Orden creada",
                # Same instant as ``status_changed_at`` above, for the same reason
                # it is shared in ``_apply_transition``: the migration's backfill
                # reads this row for an order that never left ``confirmed``.
                created_at=now,
            )
        ]

        self.db.add(order)
        self.db.flush()  # assigns id to build the human-readable code
        order.code = f"ORD-{now.year}-{order.id:04d}"
        self.db.commit()
        self.db.refresh(order)
        return order

    def transition(
        self,
        order_id: int,
        to_status: OrderStatus,
        actor: Optional[Actor] = None,
        note: Optional[str] = None,
        payment: Optional[OrderPaymentInput] = None,
        external_invoice_id: Optional[str] = None,
        branch_scope: Optional[int] = None,
    ) -> OrderModel:
        """Validates and applies a state transition, recording the history.

        Thin wrapper: ``_transition_unlocked`` does the work, this commits it and
        notifies. The two moves the shop floor makes -- into ``in_process`` and
        into ``finished`` -- normally arrive DERIVED from an activity
        (``transition_activity``) rather than through this endpoint, and that is
        exactly why the body is split: the activity and the order's transition
        have to land in ONE transaction.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        current = OrderStatus(order.status)
        self._transition_unlocked(
            order,
            to_status,
            actor=actor,
            note=note,
            payment=payment,
            external_invoice_id=external_invoice_id,
        )
        self.db.commit()
        self.db.refresh(order)

        # Best-effort side-effect on the committed transition: notify the staff
        # that should react (admins/sellers on completion, branch operators on
        # enqueue). Never raises — a failure here can't undo the commit above.
        notify_order_transition(self.db, order, current, to_status, actor)
        # And, on the real enqueue only, tell the office if this order eats into
        # material the branch is running out of. Asked here and not inside the
        # emitter because the condition is a live inventory reading, which
        # ``resolve_plan`` is deliberately unable to make. The lookup never
        # raises (see ``alerts_for_order``): the payment is already committed.
        if current == OrderStatus.confirmed and to_status == OrderStatus.queued:
            self._notify_low_stock(order, event="queued", actor=actor)
        return order

    def _notify_low_stock(
        self, order: OrderModel, *, event: str, actor: Optional[Actor]
    ) -> None:
        """Announces the order's low material, if any. Best-effort throughout."""
        alerts = StockService(self.db).alerts_for_order(order)
        if not alerts:
            return
        notify_order_low_stock(
            self.db,
            order,
            event=event,
            branch_name=order.branch.name if order.branch else None,
            product_names=[a.product_name or a.product_code or "" for a in alerts],
            actor=actor,
        )

    def _transition_unlocked(
        self,
        order: OrderModel,
        to_status: OrderStatus,
        actor: Actor,
        note: Optional[str] = None,
        payment: Optional[OrderPaymentInput] = None,
        external_invoice_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> OrderStatus:
        """Every gate and every side-effect of a transition, WITHOUT committing.

        Returns the status the order came from, which is what the caller needs to
        notify. Uncommitted on purpose: ``transition_activity`` composes an
        activity change with the order transition it derives, and a commit in the
        middle would leave the two halves separable.

        ``now`` lets the caller share ONE instant across every record of the same
        event -- the history row, the status clock, the activity's own
        timestamps. Stamping them separately leaves them a fraction of a
        millisecond apart, and the migration's backfill (which can only read the
        history) then cannot reproduce what the service wrote.

        Verifies the actor's role for the specific transition (TRANSITION_ROLES),
        the payment on entering the queue, the reason when cancelling, and --
        closing the order -- that every applicable activity is done.

        The graph is validated FIRST, ahead of every business gate. It is also
        checked inside ``_apply_transition`` (the choke point), but a transition
        that does not exist has to report itself as invalid rather than as a
        missing reason or a missing payment.
        """
        current = OrderStatus(order.status)
        self._ensure_valid_transition(current, to_status)

        # Per-transition role validation before touching the state.
        if actor.role is not None:
            allowed = TRANSITION_ROLES.get((current, to_status), ())
            if allowed and actor.role not in (r.value for r in allowed):
                raise AuthorizationError(
                    f"Tu rol no puede ejecutar la transición "
                    f"'{current.value}' → '{to_status.value}'"
                )

        # Closing gate: the order is finished when the WORK is finished. Replaces
        # the old banding-only gate, generalized to the three activities; the
        # message names the ones still open, because "no se puede" on a shop-floor
        # panel has to say who is being waited on.
        if to_status == OrderStatus.finished:
            self._ensure_activities(order)
            pending = [
                ACTIVITY_LABELS[ActivityType(a.type)]
                for a in order.activities
                if ActivityStatus(a.status) != ActivityStatus.done
            ]
            if pending:
                raise BusinessRuleError(f"Falta terminar: {', '.join(pending)}")

        # Payment-method gate: entering the queue requires registering how the
        # client pays (at least one amount > 0). Informational only, not validated against the total.
        is_payment_capture = (
            to_status == OrderStatus.queued and current == OrderStatus.confirmed
        )
        if is_payment_capture and not _has_payment(payment):
            raise ValidationError(
                "Registra la forma de pago (efectivo, transferencia y/o crédito) "
                "para enviar a cola"
            )

        # Invoice gate, the twin of the one above and for the same reason: the
        # way into the queue is the moment the sale is actually collected, so
        # that is where the billing document gets stitched to the order. Reuses
        # ``external_invoice_id`` -- there is no second invoice field, and the
        # existing ``POST /orders/{id}/invoice`` stays for corrections.
        # An order that already carries one passes without resending it.
        invoice = (external_invoice_id or "").strip()
        if is_payment_capture:
            if not invoice and not order.external_invoice_id:
                raise ValidationError(
                    "Indica el número de factura para enviar a cola",
                    field="externalInvoiceId",
                )
            # Checked HERE and written below, for the same reason the graph
            # check was hoisted into this function: every gate has to run
            # before ``_apply_transition``. Raising from the effects block
            # leaves the order already moved in the session -- uncommitted, but
            # visible to anything still reading through that session.
            if invoice:
                self._ensure_invoice_unclaimed(order, invoice)

        # Cancelling is the only move that kills a sale, and the ONLY record of
        # why is the history row's ``note``: there is no cancellation reason nor
        # actor column. Required on BOTH origins -- from ``confirmed`` the quote
        # died, from ``queued`` there is money already collected.
        if to_status == OrderStatus.cancelled and not (note or "").strip():
            raise ValidationError("Indica el motivo de la cancelación")

        now = now or datetime.utcnow()
        self._apply_transition(order, to_status, actor=actor, note=note, now=now)

        # Assignment when the shop takes the order; cleared when it goes back to
        # the queue. The rollback also reopens the CUT: it undoes somebody taking
        # the wrong order, so the cut has not started after all. The other
        # activities are left alone -- their clocks are sealed-once, and wiping
        # progress the bander declared would be destructive.
        if to_status == OrderStatus.in_process and current == OrderStatus.queued:
            order.assigned_to_id = actor.user_id
            order.assigned_at = now
            order.assigned_to_label = actor.label
        elif to_status == OrderStatus.queued and current == OrderStatus.in_process:
            order.assigned_to_id = None
            order.assigned_at = None
            order.assigned_to_label = None
            cut = self._activity(order, ActivityType.cutting)
            if cut is not None:
                cut.status = ActivityStatus.pending.value
                cut.started_at = None
                cut.started_by = None
                cut.started_by_label = None

        # Dispatch: freezes date and who handed it over (shown on the delivery block).
        if to_status == OrderStatus.dispatched:
            order.dispatched_at = now
            order.dispatched_by = actor.user_id
            order.dispatched_by_label = actor.label

        # Payment method: freezes the amounts on entering the queue (informational).
        # The admin in_process → queued rollback doesn't hit this (current != confirmed).
        # ``queued_at`` rides the same guard on purpose: it is the workshop's arrival
        # time (the board's FIFO reads it), and the rollback undoes somebody taking the
        # wrong order -- re-dating it there would send the client to the back of the
        # line for a mistake that was not theirs. The cut's ``ready_at`` is the same
        # instant: reaching the queue is when it stopped being blocked.
        if is_payment_capture:
            order.payment_cash_amount = payment.cash_amount
            order.payment_transfer_amount = payment.transfer_amount
            order.payment_credit_amount = payment.credit_amount
            if invoice:
                self._assign_external_invoice(order, invoice)
            order.queued_at = now
            cut = self._activity(order, ActivityType.cutting)
            if cut is not None and cut.ready_at is None:
                cut.ready_at = order.queued_at

        # Cancelling has NO side-effect block, and that is deliberate.
        # ``payment_*_amount`` and ``queued_at`` survive: they are the record of
        # what was collected and of when the order reached the shop, and wiping
        # them would turn a cancellation into a forged history. The activities
        # stay ``pending`` (unreachable anyway -- ``ACTIVITY_MUTABLE_ORDER_STATUSES``
        # is ``{in_process}`` -- and their clocks are sealed-once), and
        # ``assigned_to_*`` is already NULL in ``queued``. The pre-order is not
        # reopened either: re-quoting is ``POST /preorders/{id}/duplicate``.
        return current

    def get_cutting_plan(
        self, order_id: int, branch_scope: Optional[int] = None
    ) -> CuttingPlanResponse:
        """Cutting plan for the workshop view: physical boards + progress."""
        order = self.get_scoped_or_404(order_id, branch_scope)
        self._ensure_cutting_plan(order)
        boards = [
            OrderBoardResponse(
                id=board.id,
                sheet_number=board.sheet_number,
                material_key=board.material_key,
                product_code=board.product_code,
                product_name=board.product_name,
                width=board.width,
                height=board.height,
                thickness=board.thickness,
                half_board=board.half_board,
                progress=_progress(board.pieces),
                pieces=[_piece_response(p) for p in board.pieces],
                remainders=board.remainders or [],
                cuts=board.cuts or [],
            )
            for board in order.boards
        ]
        all_pieces = [p for board in order.boards for p in board.pieces]
        return CuttingPlanResponse(
            order_id=order.id,
            order_code=order.code,
            status=OrderStatus(order.status),
            notes=order.notes,
            progress=_progress(all_pieces),
            activities=self._activity_responses(order),
            boards=boards,
            print_labels_enabled=order.branch.print_labels_enabled,
        )

    def mark_piece_cut(
        self,
        order_id: int,
        placed_piece_id: int,
        cut: bool,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> PieceCutResponse:
        """Marks (or unmarks) a placed piece as cut, idempotently.

        Only while the CUT is in progress: before that there is nothing to cut,
        and once the operator closed the cut every piece is marked by definition
        -- unmarking one there would break the invariant the closing gate just
        checked. (The order status alone can no longer say this: ``cutting`` and
        ``cut`` are one state now.) ``actor`` records who cut it (FK + label),
        in sync with ``cut_at``.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        self._ensure_cutting_plan(order)
        self._ensure_activities(order)
        cut_activity = self._activity(order, ActivityType.cutting)
        if (
            OrderStatus(order.status) not in ACTIVITY_MUTABLE_ORDER_STATUSES
            or cut_activity is None
            or ActivityStatus(cut_activity.status) != ActivityStatus.in_progress
        ):
            raise BusinessRuleError(
                "Solo se pueden marcar piezas con el corte en proceso"
            )
        piece = self.db.get(OrderPlacedPieceModel, placed_piece_id)
        if piece is None or piece.order_id != order.id:
            raise EntityNotFoundError("OrderPlacedPiece", placed_piece_id)
        if cut and piece.cut_at is None:
            piece.cut_at = datetime.utcnow()
            piece.cut_by = actor.user_id
            piece.cut_by_label = actor.label
            # This piece may be the one that UNBLOCKS the bander: banding waits
            # for the first BANDED piece, the additional work for the first one
            # carrying a workshop code. Sealed once -- unmarking the piece does
            # not give back the time already waited.
            self._seal_activity_ready(order, piece)
        elif not cut:
            piece.cut_at = None
            piece.cut_by = None
            piece.cut_by_label = None
        self.db.commit()
        self.db.refresh(piece)
        all_pieces = [p for board in order.boards for p in board.pieces]
        return PieceCutResponse(
            piece=_piece_response(piece),
            progress=_progress(all_pieces),
            board_progress=_progress(piece.board.pieces),
        )

    def list_workshop_queue(
        self, branch_scope: Optional[int] = None
    ) -> List[WorkshopQueueItem]:
        """Shared shop-floor board: the queue plus the orders in process.

        Self-sufficient card list for the operator and the bander (the latter has
        no ``orders:read``): embeds the client, board/banding usage per material
        type and the activities so both can drive their actions -- take, cut,
        band, register the additional work -- from one place. Branch-isolated.

        ``activities`` carries a per-activity progress, and it is what tells the
        card whether an activity may start or finish (and, when not, how many
        pieces are still missing). The per-piece data lives in the cutting plan,
        an endpoint the bander cannot even reach -- and one request per card
        every 15s -- so the aggregates ride here.

        Prioritized orders come first, then FIFO. The shop works in arrival order
        and that stays the rule -- ``is_priority`` is the deliberate exception
        sales marks for an urgent client, and FIFO is what breaks the tie within
        each group. The sort spans both board statuses rather than only
        ``queued``: an urgent order already being cut is still the one to watch.

        **Arrival is ``queued_at``, not ``created_at``.** An order reaches the shop
        when it is PAID (``confirmed -> queued`` is gated on the payment), so a quote
        raised first can easily be paid last; ordering by creation handed the first
        slot to whoever asked first rather than whoever paid first. ``COALESCE`` keeps
        the old behavior for any row the backfill could not date, and ``id`` is the
        final tiebreak so two orders queued in the same instant stay deterministic.
        """
        query = self.db.query(OrderModel).filter(
            OrderModel.status.in_([s.value for s in WORKSHOP_QUEUE_STATUSES]),
        )
        # The branch supplies each card's printing switch; eager-load it so the
        # admin's board (which spans every branch) doesn't fire one query per row.
        query = query.options(
            joinedload(OrderModel.branch), selectinload(OrderModel.activities)
        )
        query = self._apply_branch_scope(query, branch_scope, None)
        orders = query.order_by(
            OrderModel.is_priority.desc(),
            func.coalesce(OrderModel.queued_at, OrderModel.created_at).asc(),
            OrderModel.id.asc(),
        ).all()
        order_ids = [o.id for o in orders]
        progress_by_order = self._set_progress_by_order(order_ids)
        items = []
        for o in orders:
            snapshot = o.optimization_snapshot or {}
            progress = progress_by_order.get(o.id) or _zero_set_progress()
            items.append(
                WorkshopQueueItem(
                    order_id=o.id,
                    order_code=o.code,
                    status=OrderStatus(o.status),
                    notes=o.notes,
                    is_priority=o.is_priority,
                    created_at=o.created_at,
                    queued_at=o.queued_at,
                    status_changed_at=o.status_changed_at,
                    client=ClientResponse.model_validate(o.client),
                    board_usage=_board_usage(snapshot),
                    banding_usage=_banding_usage(snapshot),
                    progress=progress["all"],
                    activities=_activity_responses(o.activities, progress),
                    print_consolidated_enabled=o.branch.print_consolidated_enabled,
                )
            )
        return items

    def _set_progress_by_order(
        self, order_ids: List[int]
    ) -> dict[int, dict[str, CuttingProgress]]:
        """Cut/total counts of EVERY piece set, per order, in one grouped query.

        One pass over ``order_placed_pieces`` answers the three sets at once
        (``_PIECE_SET_FILTERS``): a conditional ``count`` per set, where
        ``count`` skips the NULLs ``case`` yields for a piece outside it. The
        board renders one card per order, so a query per set per card would be
        the N+1 this exists to avoid. Orders with no materialized pieces are
        absent from the map (caller -> ``_zero_set_progress``).
        """
        if not order_ids:
            return {}
        piece_id = OrderPlacedPieceModel.id
        is_cut = OrderPlacedPieceModel.cut_at.isnot(None)
        columns = []
        for name, predicate in _PIECE_SET_FILTERS.items():
            if predicate is None:
                total = func.count(piece_id)
                cut = func.count(OrderPlacedPieceModel.cut_at)
            else:
                total = func.count(case((predicate(), piece_id)))
                cut = func.count(case((and_(predicate(), is_cut), piece_id)))
            columns += [total.label(f"{name}_total"), cut.label(f"{name}_cut")]
        rows = (
            self.db.query(OrderPlacedPieceModel.order_id, *columns)
            .filter(OrderPlacedPieceModel.order_id.in_(order_ids))
            .group_by(OrderPlacedPieceModel.order_id)
            .all()
        )
        return {
            row.order_id: {
                name: CuttingProgress(
                    cut_pieces=getattr(row, f"{name}_cut"),
                    total_pieces=getattr(row, f"{name}_total"),
                )
                for name in _PIECE_SET_FILTERS
            }
            for row in rows
        }

    def _set_progress(self, order_id: int) -> dict[str, CuttingProgress]:
        """``_set_progress_by_order`` for one order: every set's counts.

        The floors and the activity responses read this. Banded pieces are told
        apart by ``edges`` (copied verbatim from the snapshot) and worked ones by
        the workshop-code columns, so the table itself answers both questions.
        """
        return (
            self._set_progress_by_order([order_id]).get(order_id)
            or _zero_set_progress()
        )

    def transition_activity(
        self,
        order_id: int,
        activity_type: ActivityType,
        to_status: ActivityStatus,
        actor: Optional[Actor] = None,
        note: Optional[str] = None,
        branch_scope: Optional[int] = None,
    ) -> ActivityResult:
        """Advances one activity (``in_progress``/``done``), idempotently.

        This is the shop floor's single endpoint, and the order's own status is
        DERIVED from it: starting the cut takes the order out of the queue, and
        closing the last applicable activity finishes it. Both derived
        transitions ride the same transaction as the activity, so an order can
        never be left claiming work that is not registered.

        The three activities run in PARALLEL -- the bander works on the pieces
        the operator releases, without waiting for the whole board -- but not
        INDEPENDENTLY: each one's floors are measured against its own piece set
        (``ACTIVITY_PIECES``). Starting needs one of its pieces cut, finishing
        needs them all -- the banded pieces for the banding, the ones carrying a
        workshop code for the additional work. Without the start floor the
        bander could open an order the instant the operator took it and declare
        the work done with nothing cut, and ``done`` is terminal: it would
        satisfy the closing gate for good.

        Forward-only; re-applying the current status is a no-op. Seals
        start/finish with a timestamp + actor.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        self._ensure_activities(order)
        label = ACTIVITY_LABELS[activity_type]

        if actor.role is not None and actor.role not in (
            r.value for r in ACTIVITY_ROLES[activity_type]
        ):
            raise AuthorizationError(f"Tu rol no puede registrar el {label}")

        activity = self._activity(order, activity_type)
        # A missing row is the old ``not_applicable``: the activity does not
        # apply to this order, so there is nothing to move and nothing to gate.
        if activity is None:
            raise BusinessRuleError(f"Esta orden no lleva {label}")

        current = ActivityStatus(activity.status)
        from_status: Optional[OrderStatus] = None
        # Idempotent: re-applying the current status is a no-op (timestamps aren't re-sealed).
        if to_status != current:
            if to_status not in ACTIVITY_TRANSITIONS.get(current, set()):
                raise BusinessRuleError(
                    f"Transición de {label} inválida de '{current.value}' a "
                    f"'{to_status.value}'"
                )
            starting_the_cut = (
                activity_type == ActivityType.cutting
                and to_status == ActivityStatus.in_progress
            )
            order_status = OrderStatus(order.status)
            # Order gate: the work happens while the order is in process.
            # Starting the cut is the exception -- it is what takes the order out
            # of the queue, so it is also allowed from there.
            if order_status not in ACTIVITY_MUTABLE_ORDER_STATUSES and not (
                starting_the_cut and order_status == OrderStatus.queued
            ):
                raise BusinessRuleError(
                    f"El {label} solo se registra con la orden en proceso"
                )
            # Piece floors: checked AFTER the transition table so an invalid jump
            # still reports itself as invalid rather than as missing pieces.
            self._ensure_cutting_plan(order)
            self._check_activity_floor(order, activity_type, to_status)

            now = datetime.utcnow()
            if to_status == ActivityStatus.in_progress:
                activity.started_at = now
                activity.started_by = actor.user_id
                activity.started_by_label = actor.label
            elif to_status == ActivityStatus.done:
                activity.finished_at = now
                activity.finished_by = actor.user_id
                activity.finished_by_label = actor.label
            activity.status = to_status.value

            # Derived transitions of the ORDER, in the same transaction.
            if starting_the_cut and order_status == OrderStatus.queued:
                from_status = self._transition_unlocked(
                    order, OrderStatus.in_process, actor=actor, note=note, now=now
                )
            elif to_status == ActivityStatus.done and all(
                ActivityStatus(a.status) == ActivityStatus.done
                for a in order.activities
            ):
                from_status = self._transition_unlocked(
                    order, OrderStatus.finished, actor=actor, note=note, now=now
                )
            self.db.commit()
            self.db.refresh(order)
            self.db.refresh(activity)
            if from_status is not None:
                # Best-effort, post-commit, exactly like ``transition``.
                notify_order_transition(
                    self.db, order, from_status, OrderStatus(order.status), actor
                )

        return ActivityResult(
            order_id=order.id,
            order_code=order.code,
            order_status=OrderStatus(order.status),
            activity=self._activity_response(order, activity),
        )

    def _check_activity_floor(
        self,
        order: OrderModel,
        activity_type: ActivityType,
        to_status: ActivityStatus,
    ) -> None:
        """Raises unless the activity's own pieces allow the move.

        Table-driven on purpose (``ACTIVITY_PIECES`` +
        ``ACTIVITY_START_NEEDS_A_CUT_PIECE`` +
        ``ACTIVITY_FINISH_NEEDS_EVERY_PIECE``): the rule is one idea applied to
        three piece sets, and writing it as three ``if`` blocks is how the sets
        and the floors drift apart.
        """
        piece_set = ACTIVITY_PIECES[activity_type]
        # "" for every piece, so the cut's messages read as they always did.
        which = _PIECE_SET_QUALIFIER[piece_set]
        if to_status == ActivityStatus.in_progress:
            if not ACTIVITY_START_NEEDS_A_CUT_PIECE[activity_type]:
                return
            progress = self._set_progress(order.id)[piece_set]
            if progress.cut_pieces == 0:
                raise BusinessRuleError(f"Aún no se ha cortado ninguna pieza{which}")
            return
        if to_status == ActivityStatus.done:
            if not ACTIVITY_FINISH_NEEDS_EVERY_PIECE[activity_type]:
                return
            progress = self._set_progress(order.id)[piece_set]
            pending = progress.total_pieces - progress.cut_pieces
            if pending:
                raise BusinessRuleError(f"Faltan {pending} pieza(s){which} por cortar")

    def _activity(
        self, order: OrderModel, activity_type: ActivityType
    ) -> Optional[OrderActivityModel]:
        """The order's row for one activity, or ``None`` when it does not apply."""
        return next(
            (a for a in order.activities if a.type == activity_type.value), None
        )

    def _ensure_activities(self, order: OrderModel) -> None:
        """Materializes the activity rows from the snapshot if they are missing.

        Twin of ``_ensure_cutting_plan``: covers whatever the migration could
        not build and any order created by an older process, so no read or
        registration ever finds an order without its activities.
        """
        if order.activities:
            return
        order.activities = _build_activities(order.optimization_snapshot or {})
        if order.activities:
            self.db.commit()
            self.db.refresh(order)

    def _seal_activity_ready(
        self, order: OrderModel, piece: OrderPlacedPieceModel
    ) -> None:
        """Seals ``ready_at`` on the activities this freshly cut piece unblocks.

        Sealed once and never undone: unmarking the piece does not give back the
        time somebody already waited (same reason ``queued_at`` survives the
        rollback).
        """
        for activity in order.activities:
            activity_type = ActivityType(activity.type)
            if activity.ready_at is not None:
                continue
            if not ACTIVITY_START_NEEDS_A_CUT_PIECE[activity_type]:
                continue
            if not _PIECE_IN_SET[ACTIVITY_PIECES[activity_type]](piece):
                continue
            activity.ready_at = piece.cut_at

    def _activity_response(
        self, order: OrderModel, activity: OrderActivityModel
    ) -> OrderActivityResponse:
        """One activity with the progress of ITS pieces (one aggregate query)."""
        return _activity_responses([activity], self._set_progress(order.id))[0]

    def _activity_responses(self, order: OrderModel) -> List[OrderActivityResponse]:
        """Every activity of the order, with each one's piece progress."""
        self._ensure_activities(order)
        return _activity_responses(order.activities, self._set_progress(order.id))

    def _ensure_cutting_plan(self, order: OrderModel) -> None:
        """Materializes the cutting plan from the snapshot if it doesn't exist yet.

        Covers orders created before this feature without a backfill: the
        first read/mark rebuilds the rows from ``layouts``.
        """
        if order.boards:
            return
        _attach_cutting_plan(order, order.optimization_snapshot or {})
        if order.boards:
            self.db.commit()
            self.db.refresh(order)

    @staticmethod
    def _ensure_valid_transition(current: OrderStatus, to_status: OrderStatus) -> None:
        """Rejects a move the state machine does not have (``BusinessRuleError``).

        Called twice on purpose: once at the top of ``_transition_unlocked``, so
        the business gates (reason, payment, open activities) never answer for a
        transition that does not exist, and once here, so the choke point stays
        safe for any future caller.
        """
        if to_status not in TRANSITIONS.get(current, set()):
            raise BusinessRuleError(
                f"Transición inválida de '{current.value}' a '{to_status.value}'"
            )

    def _apply_transition(
        self,
        order: OrderModel,
        to_status: OrderStatus,
        actor: Actor,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Validates and applies the transition without committing (caller persists).

        Lets the transition be composed with other changes (e.g. marking the
        review link as used) in a single atomic transaction.
        """
        current = OrderStatus(order.status)
        self._ensure_valid_transition(current, to_status)
        # One instant for every record of the same event: the history row, the
        # clock the listing reads and -- when the transition was derived from an
        # activity -- that activity's own timestamps. Stamping them separately left
        # them a fraction of a millisecond apart, which made the migration's
        # backfill (which can only read the history) unable to reproduce what the
        # service had written.
        now = now or datetime.utcnow()
        order.history.append(
            OrderStatusHistoryModel(
                from_status=current.value,
                to_status=to_status.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note=note,
                created_at=now,
            )
        )
        order.status = to_status.value
        # The clock the listing and the board read. It lives here and not in
        # ``transition`` on purpose: this is the single choke point of every real
        # status change, so ``set_priority``/``change_branch`` -- which append a
        # ``from == to`` audit row without moving the status -- cannot restart it.
        order.status_changed_at = now

    def set_external_invoice_id(
        self,
        order_id: int,
        external_invoice_id: str,
        branch_scope: Optional[int] = None,
    ) -> OrderModel:
        """Associates (billing stitch) the external invoice ID with the order.

        Idempotent with the same ID; if a different one is already associated,
        raises ``ConflictError`` to avoid overwriting an already-issued invoice.
        """
        order = self.get_scoped_or_404(order_id, branch_scope)
        self._assign_external_invoice(order, external_invoice_id)
        self.db.commit()
        self.db.refresh(order)
        return order

    @staticmethod
    def _ensure_invoice_unclaimed(order: OrderModel, value: str) -> None:
        """Refuses a value that would overwrite a DIFFERENT issued invoice.

        Split from the write so the queue gate can check before anything moves
        and still share ONE definition of "already invoiced" with the billing
        endpoint: re-sending the same id is a no-op, another one is a conflict,
        and an issued invoice is never silently replaced.
        """
        if order.external_invoice_id is not None and order.external_invoice_id != value:
            raise ConflictError(
                "La orden ya tiene una factura externa asociada "
                f"({order.external_invoice_id})"
            )

    @staticmethod
    def _assign_external_invoice(order: OrderModel, value: str) -> None:
        """Writes the invoice id, refusing to overwrite a different one."""
        OrderService._ensure_invoice_unclaimed(order, value)
        order.external_invoice_id = value

    def change_branch(
        self,
        order_id: int,
        target_branch_id: int,
        actor: Optional[Actor] = None,
        note: Optional[str] = None,
        branch_scope: Optional[int] = None,
    ) -> OrderModel:
        """Reassigns the order to another branch (load rebalancing on saturation).

        Only allowed before the shop floor starts (``confirmed``/``queued``): in
        those states there is no assigned operator nor cut pieces, so it's a
        single write with no orphans. Documents reprint under the new branch
        automatically (the letterhead is a live lookup; the snapshot has no
        branch). If the order was already ``queued``, BOTH shop floors are told:
        it arrived at the destination, it left the origin.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        current = OrderStatus(order.status)
        if current not in (OrderStatus.confirmed, OrderStatus.queued):
            raise BusinessRuleError(
                "Solo se puede cambiar la sucursal de una orden en 'confirmed' o "
                "'queued'"
            )
        # Validates the target exists and is active (reuses the create-time resolver).
        target = resolve_branch_for_create(self.db, None, target_branch_id)
        if target == order.branch_id:
            return order  # idempotent: same branch, no-op
        # Invariant: a single active identical order per branch. The order's own
        # frozen values complete the dedupe key (services + subtotal + total),
        # so moving it only collides with an order that really is the same bill.
        dup = self._find_active_duplicate(
            target,
            order.client_id,
            order.optimization_hash,
            order.additional_services_total or 0.0,
            order.subtotal or 0.0,
            order.total or 0.0,
        )
        if dup is not None and dup.id != order.id:
            raise ConflictError(
                "La sucursal destino ya tiene una orden activa idéntica"
            )
        old_branch = order.branch_id
        # Branch NAMES for the notification copy, read BEFORE the commit: after
        # it the instances are expired and ``old_branch`` is no longer reachable
        # through the order. The target is already in the identity map
        # (``resolve_branch_for_create`` fetched it), so this costs one SELECT.
        old_branch_name = self._branch_name(old_branch)
        target_branch_name = self._branch_name(target)
        order.branch_id = target
        # Audit: a history row with from == to (not a state transition) + note.
        order.history.append(
            OrderStatusHistoryModel(
                from_status=current.value,
                to_status=current.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note=note or f"Sucursal cambiada de {old_branch} a {target}",
            )
        )
        self.db.commit()
        self.db.refresh(order)
        # Best-effort: tells both shop floors. Whether the move is worth
        # announcing at all is the emitter's call, like every other "who hears
        # about this" question.
        notify_order_branch_changed(
            self.db,
            order,
            order_status=current,
            from_branch_id=old_branch,
            to_branch_id=target,
            from_branch_name=old_branch_name,
            to_branch_name=target_branch_name,
            actor=actor,
        )
        return order

    def _branch_name(self, branch_id: int) -> Optional[str]:
        """Readable name of a branch (``None`` if it is gone)."""
        branch = self.db.get(BranchModel, branch_id)
        return branch.name if branch else None

    def set_priority(
        self,
        order_id: int,
        is_priority: bool,
        actor: Optional[Actor] = None,
        note: Optional[str] = None,
        branch_scope: Optional[int] = None,
    ) -> OrderModel:
        """Marks (or unmarks) the order for priority attention on the board.

        Sales' escape hatch from FIFO: an urgent client gets attended first
        without touching the status machine, the snapshot or a single price --
        the flag only moves the order to the head of ``list_workshop_queue`` and
        lights the card up. Reversible, so a wrongly marked order is one call
        away from its place in the queue.

        Refused once the order is closed (``TERMINAL_STATUSES``): prioritizing a
        dispatched order means nothing, and the board doesn't list it anyway.
        Idempotent -- re-marking writes no history row, so a double click doesn't
        fill the timeline with noise.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        current = OrderStatus(order.status)
        if current in TERMINAL_STATUSES:
            raise BusinessRuleError(
                "No se puede cambiar la prioridad de una orden cerrada"
            )
        if order.is_priority == is_priority:
            return order  # idempotent: same value, no-op (and no history row)
        order.is_priority = is_priority
        # Audit: a history row with from == to (not a state transition) + note,
        # the same shape ``change_branch`` uses to record a non-transition.
        order.history.append(
            OrderStatusHistoryModel(
                from_status=current.value,
                to_status=current.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note=note
                or (
                    "Marcada como prioritaria" if is_priority else "Prioridad retirada"
                ),
            )
        )
        self.db.commit()
        self.db.refresh(order)
        return order

    def build_export(
        self, order_id: int, branch_scope: Optional[int] = None
    ) -> OrderExportResponse:
        """Projects the order as a neutral billing document (billing=boards)."""
        order = self.get_scoped_or_404(order_id, branch_scope)
        lines = [
            OrderExportLine(
                description=_line_description(line),
                product_code=line.product_code,
                quantity=line.quantity,
                unit_price=line.unit_price_snapshot,
                line_total=line.line_total,
            )
            for line in order.lines
        ]
        return OrderExportResponse(
            order_code=order.code,
            status=OrderStatus(order.status),
            issued_at=order.confirmed_at or order.created_at,
            currency=order.currency,
            client=order.client,
            lines=lines,
            subtotal=order.subtotal,
            price_level=order.price_level,
            tax_rate=order.tax_rate,
            tax_amount=order.tax_amount,
            total=order.total,
            external_invoice_id=order.external_invoice_id,
        )

    def _find_active_duplicate(
        self,
        branch_id: int,
        client_id: int,
        optimization_hash: str,
        additional_services_total: float = 0.0,
        subtotal: float = 0.0,
        total: float = 0.0,
    ) -> Optional[OrderModel]:
        """Non-terminal order from the same branch+client with the same hash (idempotency).

        Includes the branch in the key: the same client can order the same thing
        at two branches and those are different orders. Everything commercial
        lives outside the hash — the price level, which boards are marked for it,
        ``wholeBoard``, the services — and every one of them moves the
        **subtotal**, so that single number stands in for all of them. ``total``
        joins it to catch a change in the tax rate, which moves nothing else, and
        the services total stays as its own column because it is the one that is
        also queried on its own. Two genuinely different selections that happen
        to add up to the same money still collapse into one order — the same
        approximation this key has always accepted.
        """
        terminal = [s.value for s in TERMINAL_STATUSES]
        return (
            self.db.query(OrderModel)
            .filter(
                OrderModel.branch_id == branch_id,
                OrderModel.client_id == client_id,
                OrderModel.optimization_hash == optimization_hash,
                OrderModel.additional_services_total == additional_services_total,
                OrderModel.subtotal == subtotal,
                OrderModel.total == total,
                OrderModel.status.not_in(terminal),
            )
            .first()
        )


def _with_workshop_codes(
    dumped: List[dict], requirements: List[Requirement]
) -> List[dict]:
    """The payload's requirements with each one's workshop codes put back.

    Paired BY POSITION: ``_build_result_payload`` dumps one entry per request
    requirement, in order -- the same pairing the ``order_pieces`` backfill
    relies on. A new list of new dicts, so a payload that came out of the cache
    is never mutated.
    """
    return [
        {**r, **{f: getattr(req, f) for f in WORKSHOP_CODE_FIELDS}}
        for r, req in zip(dumped, requirements, strict=True)
    ]


def _workshop_codes_by_instance(
    requirements: List[dict],
) -> dict[Tuple[str, str], dict]:
    """``(material_key, instance_id) -> codes`` for every piece that carries any.

    Instance ids are only unique inside ONE material group (two materials can
    both cut a "Puerta"), so the group is part of the key, and each group is
    named exactly as the optimizer named it: same grouping (by ``material_key``,
    request order) and the same ``piece_instance_ids``.
    """
    groups: dict[str, List[dict]] = {}
    for r in requirements:
        groups.setdefault(r.get("material_key"), []).append(r)
    out: dict[Tuple[str, str], dict] = {}
    for key, reqs in groups.items():
        entries = [(r.get("label"), r.get("quantity", 1)) for r in reqs]
        for i, uid in piece_instance_ids(entries):
            if has_workshop_codes(reqs[i]):
                out[(key, uid)] = {f: reqs[i].get(f) for f in WORKSHOP_CODE_FIELDS}
    return out


def _attach_cutting_plan(
    order: OrderModel, payload: dict, anchor_of: Optional[dict[str, str]] = None
) -> None:
    """Expands ``payload["layouts"]`` into physical boards + placed pieces.

    Each layout in the snapshot is a real sheet; ``sheet_number`` is
    reassigned as a global sequence (the snapshot's resets per material).
    Pieces are also linked directly to the order to count progress without joins.

    Each placed piece also takes the workshop codes of the cut-list row it came
    from. ``anchor_of`` maps a material to the anchor whose cut list its pieces
    belong to (a pooled retazo cuts its anchor's pieces); without it every
    material is its own anchor, which is exact for any job with no pool and
    harmless for the orders the lazy path rebuilds, none of which carry codes.
    """
    anchor_of = anchor_of or {}
    codes_by_instance = _workshop_codes_by_instance(payload.get("requirements") or [])
    materials_by_key = {m["material_key"]: m for m in payload.get("materials", [])}
    for seq, layout in enumerate(payload.get("layouts", []), start=1):
        material = layout.get("material", {})
        resolved = materials_by_key.get(material.get("material_key"), {})
        anchor = anchor_of.get(material.get("material_key")) or material.get(
            "material_key"
        )
        board = OrderBoardModel(
            sheet_number=seq,
            material_key=material.get("material_key", ""),
            product_id=resolved.get("product_id"),
            product_code=resolved.get("product_code"),
            product_name=resolved.get("product_name"),
            width=material.get("width", 0.0),
            height=material.get("height", 0.0),
            thickness=material.get("thickness", 0.0),
            half_board=bool(material.get("half_board", False)),
            remainders=layout.get("remainders") or None,
            # Snapshots predating ``cuts`` serialization don't carry the key.
            cuts=layout.get("cuts") or None,
        )
        for placed in layout.get("placed_pieces", []):
            piece_id = str(placed.get("piece_id", ""))
            codes = codes_by_instance.get((anchor, piece_id), {})
            board.pieces.append(
                OrderPlacedPieceModel(
                    order=order,
                    piece_id=piece_id,
                    label=base_label(piece_id),
                    x=placed["x"],
                    y=placed["y"],
                    width=placed["width"],
                    height=placed["height"],
                    original_width=placed.get("original_width", placed["width"]),
                    original_height=placed.get("original_height", placed["height"]),
                    rotated=bool(placed.get("rotated", False)),
                    edges=placed.get("edges"),
                    **codes,
                )
            )
        order.boards.append(board)


def _material_name(resolved: Optional[dict], line: dict) -> str:
    """Display name of a material, always the WHOLE board's.

    Same rule ``build_materials_summary`` applies to its ``base_name``
    (``product_name`` or the dimensions), but read off the resolved material
    instead of the summary line: that one belongs to the full sheet, so it
    never carries the ``" (medio tablero)"`` suffix, and an inline material
    yields the whole lamina's ``2440×2070`` rather than the already-halved
    ``1220×2070``. Falls back to the summary line for a snapshot whose
    ``materials`` has no entry for the key.
    """
    if resolved:
        name = resolved.get("product_name")
        if name:
            return name
        width = resolved.get("width")
        height = resolved.get("height")
        if width and height:
            return f"{width:g}×{height:g}"
    return (
        line.get("product_name") or line.get("product_code") or line.get("material_key")
    )


def _board_usage(snapshot: dict) -> List[dict]:
    """Sheet count per MATERIAL, from the already-loaded snapshot (no extra query).

    One entry per ``material_key``. ``materials_summary`` is keyed by
    ``(material_key, half_board)``, so a material billed as full boards plus a
    half board arrives as two lines that share every identity field and differ
    only in the suffix baked into ``product_name`` -- which read on the board as
    two different products, and made the dialog's footer count them as two
    materials. They are merged here and the split is reported as
    ``full_count``/``half_count`` instead.

    First-appearance order is preserved, which is the order the shop cuts in
    (``patterns.order_sheets`` puts a pool's whole boards first and its half
    last), so a material is named by its whole sheet whenever it has one.
    """
    resolved = {m.get("material_key"): m for m in snapshot.get("materials", [])}
    usage: dict = {}
    for line in snapshot.get("materials_summary", []):
        key = line.get("material_key")
        entry = usage.get(key)
        if entry is None:
            entry = usage[key] = {
                "material_key": key,
                "name": _material_name(resolved.get(key), line),
                "count": 0,
                "full_count": 0,
                "half_count": 0,
            }
        count = line.get("count", 0)
        entry["count"] += count
        entry["half_count" if line.get("half_board") else "full_count"] += count
    return list(usage.values())


def _banding_usage(snapshot: dict) -> List[dict]:
    """Billed linear meters per edge-banding type, from the already-loaded snapshot.

    So the bander knows how much tapacanto to prepare. Reads the already-loaded
    ``edge_bandings_summary`` (no extra query); skips geometry-only entries with
    no product. ``band_type`` travels as the canonical ``Soft``/``Hard`` rather
    than folded into the name as ``"(Suave)"``: the workshop board paints it as
    a badge, and translating it here would leave the client no way to tell the
    type apart from the product's own name.
    """
    usage: List[dict] = []
    for e in snapshot.get("edge_bandings_summary", []):
        name = e.get("product_name")
        if not name:
            continue
        usage.append(
            {
                "name": name,
                "band_type": e.get("band_type"),
                "linear_m": e.get("billed_linear_m", 0),
            }
        )
    return usage


def _is_banded():
    """SQL predicate for "this placed piece carries edge banding".

    ``edges`` is a plain ``JSON`` column and SQLAlchemy persists Python ``None``
    into it as the JSON value ``null``, not as SQL NULL -- so ``IS NOT NULL`` is
    true for EVERY row and would count plain pieces as banded (measured: the gate
    opened on a piece with no banding at all). ``json_typeof`` tells the two
    apart, and folds in the SQL-NULL case for free: it yields NULL there, which
    no comparison passes. Rows that do carry banding are objects, and
    ``EdgeBandingSpec.sides`` requires at least one side, so the type alone is
    the whole test.
    """
    return func.json_typeof(OrderPlacedPieceModel.edges) != "null"


def _piece_is_banded(piece: OrderPlacedPieceModel) -> bool:
    """In-Python twin of :func:`_is_banded`, for a piece already in the session.

    Same trap, other side of the wire: ``edges`` comes back as ``None`` for a
    plain piece whether the column held SQL NULL or the JSON value ``null``, so
    the truthiness test is the whole thing -- but it has to be written down,
    because ``piece.edges is not None`` reads like the SQL check that does not
    work.
    """
    return bool(piece.edges)


def _is_worked():
    """SQL predicate for "this placed piece carries shop work".

    A plain ``IS NOT NULL`` per code, and correct here where it is not for
    ``edges``: these are ``String`` columns, and ``Requirement`` turns a blank
    code into ``None`` before anything is stored.
    """
    return or_(
        *(getattr(OrderPlacedPieceModel, f).isnot(None) for f in WORKSHOP_CODE_FIELDS)
    )


def _piece_is_worked(piece: OrderPlacedPieceModel) -> bool:
    """In-Python twin of :func:`_is_worked`, for a piece already in the session."""
    return any(getattr(piece, f) for f in WORKSHOP_CODE_FIELDS)


# The piece sets ``ACTIVITY_PIECES`` names, three ways: the SQL filter the counts
# run under (``None`` = every piece), the in-Python membership test for a piece
# already loaded, and how a floor's message names the set to the shop floor.
_PIECE_SET_FILTERS = {"all": None, "banded": _is_banded, "worked": _is_worked}
_PIECE_IN_SET = {
    "all": lambda piece: True,
    "banded": _piece_is_banded,
    "worked": _piece_is_worked,
}
_PIECE_SET_QUALIFIER = {
    "all": "",
    "banded": " con canto",
    "worked": " con trabajo de taller",
}


def _zero_set_progress() -> dict[str, CuttingProgress]:
    """Every set at 0/0: an order whose cutting plan holds no piece at all."""
    return {
        name: CuttingProgress(cut_pieces=0, total_pieces=0)
        for name in _PIECE_SET_FILTERS
    }


def _build_activities(snapshot: dict) -> List[OrderActivityModel]:
    """The activity rows an order needs, from its frozen snapshot.

    One row per APPLICABLE activity and no row for the rest -- the cut always,
    the banding when something is billed as edge banding, the additional work
    when some piece of the cut list carries a workshop code. The billed
    additional services are deliberately NOT a source: they are lines on the
    bill, and a line on the bill is not work on the shop floor. Both sources are
    read off the snapshot, so nothing new is stored to answer "does this apply".
    """
    types = [ActivityType.cutting]
    if snapshot.get("edge_bandings_summary"):
        types.append(ActivityType.banding)
    if any(has_workshop_codes(r) for r in snapshot.get("requirements") or []):
        types.append(ActivityType.additional)
    return [
        OrderActivityModel(type=t.value, status=ActivityStatus.pending.value)
        for t in types
    ]


def _activity_responses(
    activities: List[OrderActivityModel],
    progress_by_set: dict[str, CuttingProgress],
) -> List[OrderActivityResponse]:
    """Projects activity rows, each with the progress of ITS piece set.

    Takes the aggregates already computed by the caller instead of querying per
    activity: the board renders one card per order and would otherwise fire a
    query per activity per row.
    """
    return [
        OrderActivityResponse(
            type=ActivityType(a.type),
            status=ActivityStatus(a.status),
            ready_at=a.ready_at,
            started_at=a.started_at,
            started_by=a.started_by,
            started_by_label=a.started_by_label,
            finished_at=a.finished_at,
            finished_by=a.finished_by,
            finished_by_label=a.finished_by_label,
            progress=progress_by_set[ACTIVITY_PIECES[ActivityType(a.type)]],
        )
        for a in sorted(activities, key=lambda a: a.type)
    ]


def _progress(pieces: List[OrderPlacedPieceModel]) -> CuttingProgress:
    """Cutting progress over a set of placed pieces."""
    return CuttingProgress(
        cut_pieces=sum(1 for p in pieces if p.cut_at is not None),
        total_pieces=len(pieces),
    )


def _piece_response(piece: OrderPlacedPieceModel) -> PlacedPieceResponse:
    """API projection of a placed piece (``cut`` derived from ``cut_at``)."""
    return PlacedPieceResponse(
        id=piece.id,
        piece_id=piece.piece_id,
        label=piece.label,
        x=piece.x,
        y=piece.y,
        width=piece.width,
        height=piece.height,
        original_width=piece.original_width,
        original_height=piece.original_height,
        rotated=piece.rotated,
        edges=piece.edges,
        hinging_code=piece.hinging_code,
        grooving_code=piece.grooving_code,
        assembly_code=piece.assembly_code,
        division_code=piece.division_code,
        cut=piece.cut_at is not None,
        cut_at=piece.cut_at,
        cut_by=piece.cut_by,
        cut_by_label=piece.cut_by_label,
    )


def _line_description(line: OrderLineModel) -> str:
    """Human-readable description of a billing line for the external invoice."""
    if line.product_code and line.product_name:
        return f"{line.product_name} ({line.product_code})"
    return line.product_name or line.product_code or f"Producto {line.product_id}"


def order_service(db: Session = Depends(get_db)) -> OrderService:
    """``OrderService`` provider for route injection."""
    return OrderService(db)
