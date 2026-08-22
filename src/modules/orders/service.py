from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from src.modules.branches.service import resolve_branch_for_create
from src.modules.clients.model import ClientModel
from src.modules.clients.schemas import ClientResponse
from src.modules.clients.service import require_phone
from src.modules.notifications.emitter import notify_order_transition
from src.modules.optimizations.labels import BAND_TYPE_LABEL
from src.modules.optimizations.patterns import base_label
from src.modules.optimizations.pricing import build_pricing
from src.modules.optimizations.schemas import OptimizeRequest
from src.modules.optimizations.service import OptimizationService
from src.modules.orders.model import (
    BANDING_MUTABLE_ORDER_STATUSES,
    BANDING_PENDING_STATUSES,
    BANDING_TRANSITION_ROLES,
    BANDING_TRANSITIONS,
    TERMINAL_STATUSES,
    TRANSITION_ROLES,
    TRANSITIONS,
    WORKSHOP_QUEUE_STATUSES,
    BandingStatus,
    OrderBoardModel,
    OrderLineModel,
    OrderModel,
    OrderPieceModel,
    OrderPlacedPieceModel,
    OrderStatus,
    OrderStatusHistoryModel,
)
from src.modules.orders.schemas import (
    BandingStatusResponse,
    CuttingPlanResponse,
    CuttingProgress,
    OrderBoardResponse,
    OrderCreate,
    OrderExportLine,
    OrderExportResponse,
    OrderPaymentInput,
    PieceCutResponse,
    PlacedPieceResponse,
    WorkshopQueueItem,
)
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
    """True if at least one amount (> 0) was registered in either payment method."""
    if payment is None:
        return False
    return (payment.cash_amount or 0) > 0 or (payment.credit_amount or 0) > 0


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
    ) -> Tuple[List[OrderModel], int]:
        """Lists orders with total count: ``(items, total)``.

        ``status`` filters by one or more statuses (empty list/``None`` = all).
        ``branch_scope`` isolates staff to their branch; the admin (``None``)
        sees all and can narrow to one with ``branch_filter``.

        ``search`` matches the order code or the client (identifier/first/last
        name), and also the order id when the term is all digits -- the shop
        says "order 42" as often as it reads the code off the sheet.

        ``sort`` defaults to ``oldest`` because the workshop reads this listing
        FIFO; the back office asks for ``recent`` explicitly.
        """
        query = self.db.query(OrderModel)
        if status:
            query = query.filter(OrderModel.status.in_([s.value for s in status]))
        if client_filter is not None:
            query = query.filter(OrderModel.client_id == client_filter)
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
        order_by = OrderModel.id.desc() if sort == "recent" else OrderModel.id.asc()
        orders = query.order_by(order_by).offset(offset).limit(limit).all()
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
            strategy=data.strategy,
            variant=data.variant,
        )
        payload, optimization_hash = self.optimization_service.compute(opt_request)

        # Orders accept materials outside the catalog (offcuts/manual): they're
        # frozen as-is from the snapshot. Their lines/pieces end up with a null
        # ``product_id`` and are identified by ``product_code``/``product_name``.

        # Additional services (billed on top of the total, after the discount).
        # Not cut geometry: they aren't in the hash, so like the tier they factor
        # into dedupe (two identical cuts differing only in services aren't the
        # same order).
        additional_services = [
            s.model_dump(mode="json") for s in data.additional_services
        ]
        # Price tier: validated and its rate frozen (historical audit). The
        # discount factors into dedupe because two orders that are geometrically
        # identical but at a different tier are NOT the same order (the tier
        # isn't part of the hash).
        tier = self.settings_service.resolve_price_tier(data.price_tier_code)
        pricing = build_pricing(payload, tier, additional_services)
        existing = self._find_active_duplicate(
            branch_id,
            data.client_id,
            optimization_hash,
            tier["code"],
            pricing["services_total"],
        )
        if existing is not None:
            return existing

        # Document-level discount (catalog boards only) + services. Lines are frozen
        # at list price; the snapshot embeds `pricing` and the service breakdown so
        # it's self-contained.
        snapshot = {
            **payload,
            "pricing": pricing,
            "additional_services": additional_services,
        }

        # The order is born 'confirmed' (the client's prior review, formerly
        # 'quoted', now lives in the pre-order, which mints this order on confirmation).
        # Banding track: starts 'pending' if the order has edge banding (something
        # to band), else 'not_applicable' (doesn't participate in the closing gate).
        has_banding = bool(payload.get("edge_bandings_summary"))
        banding_status = (
            BandingStatus.pending if has_banding else BandingStatus.not_applicable
        )
        now = datetime.utcnow()
        order = OrderModel(
            client_id=data.client_id,
            branch_id=branch_id,
            status=data.status.value,
            banding_status=banding_status.value,
            optimization_snapshot=snapshot,
            optimization_hash=optimization_hash,
            currency="USD",
            subtotal=pricing["subtotal"],
            total=pricing["total"],
            price_tier_code=tier["code"],
            discount_rate=tier["rate"],
            discount_amount=pricing["discount_amount"],
            additional_services_total=pricing["services_total"],
            total_boards_used=payload["total_boards_used"],
            source=data.source,
            notes=data.notes,
            created_at=now,
            confirmed_at=now,
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
        # resolved by the material's key (null if the material isn't from the catalog).
        product_id_by_key = {
            m["material_key"]: m["product_id"] for m in payload.get("materials", [])
        }
        order.pieces = [
            OrderPieceModel(
                product_id=product_id_by_key[r["material_key"]],
                label=r.get("label"),
                height=r["height"],
                width=r["width"],
                quantity=r["quantity"],
                priority=r.get("priority", 0),
                can_rotate=r.get("can_rotate", True),
                edges=r.get("edge_banding"),
            )
            for r in payload["requirements"]
        ]
        # Cutting plan = physical boards with each placed piece (the unit the
        # operator marks in the workshop; mutable state outside the snapshot).
        _attach_cutting_plan(order, payload)
        order.history = [
            OrderStatusHistoryModel(
                from_status=None,
                to_status=data.status.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note="Orden creada",
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
        branch_scope: Optional[int] = None,
    ) -> OrderModel:
        """Validates and applies a state transition, recording the history.

        Verifies the actor's role is authorized for the specific transition
        (TRANSITION_ROLES). Production gate: moving to ``cut`` requires every
        piece in the cutting plan to be marked.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        current = OrderStatus(order.status)

        # Per-transition role validation before touching the state.
        if actor.role is not None:
            allowed = TRANSITION_ROLES.get((current, to_status), ())
            if allowed and actor.role not in (r.value for r in allowed):
                raise AuthorizationError(
                    f"Tu rol no puede ejecutar la transición "
                    f"'{current.value}' → '{to_status.value}'"
                )

        # Gate: every piece must be cut before the cutting stage can close.
        if to_status == OrderStatus.cut and order.status == OrderStatus.cutting.value:
            self._ensure_cutting_plan(order)
            pending = (
                self.db.query(OrderPlacedPieceModel)
                .filter(
                    OrderPlacedPieceModel.order_id == order.id,
                    OrderPlacedPieceModel.cut_at.is_(None),
                )
                .count()
            )
            if pending:
                raise BusinessRuleError(f"Faltan {pending} pieza(s) por cortar")

        # Closing gate: if the order has edge banding, banding must be finished
        # before completing it (orders with no banding pass straight through).
        if (
            to_status == OrderStatus.completed
            and BandingStatus(order.banding_status) in BANDING_PENDING_STATUSES
        ):
            raise BusinessRuleError("Falta terminar el canteado")

        # Payment-method gate: entering the queue requires registering how the
        # client pays (at least one amount > 0). Informational only, not validated against the total.
        is_payment_capture = (
            to_status == OrderStatus.queued and current == OrderStatus.confirmed
        )
        if is_payment_capture and not _has_payment(payment):
            raise ValidationError(
                "Registra la forma de pago (efectivo y/o crédito) para enviar a cola"
            )

        self._apply_transition(order, to_status, actor=actor, note=note)

        # Assignment when transitioning to ``cutting``; cleared when returning to ``queued``.
        if to_status == OrderStatus.cutting:
            order.assigned_to_id = actor.user_id
            order.assigned_at = datetime.utcnow()
            order.assigned_to_label = actor.label
        elif to_status == OrderStatus.queued and current == OrderStatus.cutting:
            order.assigned_to_id = None
            order.assigned_at = None
            order.assigned_to_label = None

        # Dispatch: freezes date and who handed it over (shown on the dispatch sheet).
        if to_status == OrderStatus.dispatched:
            order.dispatched_at = datetime.utcnow()
            order.dispatched_by = actor.user_id
            order.dispatched_by_label = actor.label

        # Payment method: freezes the amounts on entering the queue (informational).
        # The admin cutting → queued rollback doesn't hit this (current != confirmed).
        if is_payment_capture:
            order.payment_cash_amount = payment.cash_amount
            order.payment_credit_amount = payment.credit_amount

        self.db.commit()
        self.db.refresh(order)

        # Best-effort side-effect on the committed transition: notify the staff
        # that should react (admins/sellers on completion, branch operators on
        # enqueue). Never raises — a failure here can't undo the commit above.
        notify_order_transition(self.db, order, current, to_status, actor)
        return order

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

        Only with the order in ``cutting``: before that there's nothing to cut,
        and after that the cutting stage is already closed by the transition.
        ``actor`` records who cut it (FK + label), in sync with ``cut_at``.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)
        self._ensure_cutting_plan(order)
        if order.status != OrderStatus.cutting.value:
            raise BusinessRuleError(
                "Solo se pueden marcar piezas con la orden en corte (cutting)"
            )
        piece = self.db.get(OrderPlacedPieceModel, placed_piece_id)
        if piece is None or piece.order_id != order.id:
            raise EntityNotFoundError("OrderPlacedPiece", placed_piece_id)
        if cut and piece.cut_at is None:
            piece.cut_at = datetime.utcnow()
            piece.cut_by = actor.user_id
            piece.cut_by_label = actor.label
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
        """Shared shop-floor board: orders from the queue up to "cut".

        Self-sufficient card list for the operator and the bander (the latter has
        no ``orders:read``): embeds the client, board/banding usage per material
        type and cutting progress so both can drive their actions -- take/cut,
        band, complete -- from one place. Branch-isolated, oldest first (FIFO).
        """
        query = self.db.query(OrderModel).filter(
            OrderModel.status.in_([s.value for s in WORKSHOP_QUEUE_STATUSES]),
        )
        # The branch supplies each card's printing switch; eager-load it so the
        # admin's board (which spans every branch) doesn't fire one query per row.
        query = query.options(joinedload(OrderModel.branch))
        query = self._apply_branch_scope(query, branch_scope, None)
        orders = query.order_by(OrderModel.id.asc()).all()
        progress_by_order = self._cutting_progress_by_order([o.id for o in orders])
        items = []
        for o in orders:
            snapshot = o.optimization_snapshot or {}
            items.append(
                WorkshopQueueItem(
                    order_id=o.id,
                    order_code=o.code,
                    status=OrderStatus(o.status),
                    banding_status=BandingStatus(o.banding_status),
                    notes=o.notes,
                    created_at=o.created_at,
                    client=ClientResponse.model_validate(o.client),
                    board_usage=_board_usage(snapshot),
                    banding_usage=_banding_usage(snapshot),
                    progress=progress_by_order.get(
                        o.id, CuttingProgress(cut_pieces=0, total_pieces=0)
                    ),
                    print_consolidated_enabled=o.branch.print_consolidated_enabled,
                )
            )
        return items

    def _cutting_progress_by_order(
        self, order_ids: List[int]
    ) -> dict[int, CuttingProgress]:
        """Cut/total placed-piece counts per order in a single grouped query.

        Avoids N+1: ``count(cut_at)`` tallies only non-null timestamps (cut pieces).
        Orders with no materialized pieces are absent from the map (caller → 0/0).
        """
        if not order_ids:
            return {}
        rows = (
            self.db.query(
                OrderPlacedPieceModel.order_id,
                func.count(OrderPlacedPieceModel.id).label("total"),
                func.count(OrderPlacedPieceModel.cut_at).label("cut"),
            )
            .filter(OrderPlacedPieceModel.order_id.in_(order_ids))
            .group_by(OrderPlacedPieceModel.order_id)
            .all()
        )
        return {
            row.order_id: CuttingProgress(cut_pieces=row.cut, total_pieces=row.total)
            for row in rows
        }

    def transition_banding(
        self,
        order_id: int,
        to_status: BandingStatus,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> BandingStatusResponse:
        """Advances the banding track (``in_progress``/``done``), idempotently.

        Track parallel to and independent of cutting: only requires the order
        to already be in ``cutting``/``cut`` (pieces are released to band).
        Forward-only; re-applying the current status is a no-op. Seals
        start/finish with a timestamp + actor.
        """
        actor = actor or system_actor()
        order = self.get_scoped_or_404(order_id, branch_scope)

        if actor.role is not None and actor.role not in (
            r.value for r in BANDING_TRANSITION_ROLES
        ):
            raise AuthorizationError("Tu rol no puede registrar el canteado")

        current = BandingStatus(order.banding_status)
        if current == BandingStatus.not_applicable:
            raise BusinessRuleError("Esta orden no lleva tapacantos")
        if OrderStatus(order.status) not in BANDING_MUTABLE_ORDER_STATUSES:
            raise BusinessRuleError(
                "El canteado solo se registra con la orden en corte o cortada"
            )

        # Idempotent: re-applying the current status is a no-op (timestamps aren't re-sealed).
        if to_status != current:
            if to_status not in BANDING_TRANSITIONS.get(current, set()):
                raise BusinessRuleError(
                    f"Transición de canteado inválida de '{current.value}' a "
                    f"'{to_status.value}'"
                )
            now = datetime.utcnow()
            if to_status == BandingStatus.in_progress:
                order.banding_started_at = now
                order.banding_started_by = actor.user_id
                order.banding_started_by_label = actor.label
            elif to_status == BandingStatus.done:
                order.banding_finished_at = now
                order.banding_finished_by = actor.user_id
                order.banding_finished_by_label = actor.label
            order.banding_status = to_status.value
            self.db.commit()
            self.db.refresh(order)

        return BandingStatusResponse(
            order_id=order.id,
            order_code=order.code,
            banding_status=BandingStatus(order.banding_status),
            banding_started_at=order.banding_started_at,
            banding_finished_at=order.banding_finished_at,
        )

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

    def _apply_transition(
        self,
        order: OrderModel,
        to_status: OrderStatus,
        actor: Actor,
        note: Optional[str] = None,
    ) -> None:
        """Validates and applies the transition without committing (caller persists).

        Lets the transition be composed with other changes (e.g. marking the
        review link as used) in a single atomic transaction.
        """
        current = OrderStatus(order.status)
        if to_status not in TRANSITIONS.get(current, set()):
            raise BusinessRuleError(
                f"Transición inválida de '{current.value}' a '{to_status.value}'"
            )
        order.history.append(
            OrderStatusHistoryModel(
                from_status=current.value,
                to_status=to_status.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note=note,
            )
        )
        order.status = to_status.value

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
        if (
            order.external_invoice_id is not None
            and order.external_invoice_id != external_invoice_id
        ):
            raise ConflictError(
                "La orden ya tiene una factura externa asociada "
                f"({order.external_invoice_id})"
            )
        order.external_invoice_id = external_invoice_id
        self.db.commit()
        self.db.refresh(order)
        return order

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
        branch). If the order was already ``queued``, the new branch's operators
        are notified it landed in their queue.
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
        # Invariant: a single active identical order per branch (dedupe key is
        # branch + client + hash + tier).
        dup = self._find_active_duplicate(
            target, order.client_id, order.optimization_hash, order.price_tier_code
        )
        if dup is not None and dup.id != order.id:
            raise ConflictError(
                "La sucursal destino ya tiene una orden activa idéntica"
            )
        old_branch = order.branch_id
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
        # Already in the queue: tell the NEW branch's operators (best-effort).
        if current == OrderStatus.queued:
            notify_order_transition(
                self.db, order, OrderStatus.confirmed, OrderStatus.queued, actor
            )
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
            price_tier_code=order.price_tier_code,
            discount_rate=order.discount_rate,
            discount_amount=order.discount_amount,
            total=order.total,
            external_invoice_id=order.external_invoice_id,
        )

    def _find_active_duplicate(
        self,
        branch_id: int,
        client_id: int,
        optimization_hash: str,
        price_tier_code: str,
        additional_services_total: float = 0.0,
    ) -> Optional[OrderModel]:
        """Non-terminal order from the same branch+client with the same hash (idempotency).

        Includes the branch in the key: the same client can order the same
        thing at two branches and those are different orders. Includes the
        price tier and the additional-services total: neither is part of the
        hash, so the same cut at a different tier or with different services
        counts as two orders.
        """
        terminal = [s.value for s in TERMINAL_STATUSES]
        return (
            self.db.query(OrderModel)
            .filter(
                OrderModel.branch_id == branch_id,
                OrderModel.client_id == client_id,
                OrderModel.optimization_hash == optimization_hash,
                OrderModel.price_tier_code == price_tier_code,
                OrderModel.additional_services_total == additional_services_total,
                OrderModel.status.not_in(terminal),
            )
            .first()
        )


def _attach_cutting_plan(order: OrderModel, payload: dict) -> None:
    """Expands ``payload["layouts"]`` into physical boards + placed pieces.

    Each layout in the snapshot is a real sheet; ``sheet_number`` is
    reassigned as a global sequence (the snapshot's resets per material).
    Pieces are also linked directly to the order to count progress without joins.
    """
    materials_by_key = {m["material_key"]: m for m in payload.get("materials", [])}
    for seq, layout in enumerate(payload.get("layouts", []), start=1):
        material = layout.get("material", {})
        resolved = materials_by_key.get(material.get("material_key"), {})
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
                )
            )
        order.boards.append(board)


def _board_usage(snapshot: dict) -> List[dict]:
    """Board count per material/board type, from the already-loaded snapshot.

    Reads ``materials_summary`` (no extra query) -- the same per-material
    ``count`` already printed on the production sheet's boards table.
    """
    return [
        {
            "name": e.get("product_name")
            or e.get("product_code")
            or e.get("material_key"),
            "count": e.get("count", 0),
        }
        for e in snapshot.get("materials_summary", [])
    ]


def _banding_usage(snapshot: dict) -> List[dict]:
    """Billed linear meters per edge-banding type, from the already-loaded snapshot.

    So the bander knows how much tapacanto to prepare. Reads the already-loaded
    ``edge_bandings_summary`` (no extra query); skips geometry-only entries with
    no product, and omits the type suffix when the band type is unknown.
    """
    usage: List[dict] = []
    for e in snapshot.get("edge_bandings_summary", []):
        name = e.get("product_name")
        if not name:
            continue
        label = BAND_TYPE_LABEL.get(e.get("band_type"))
        display = f"{name} ({label})" if label else name
        usage.append({"name": display, "linear_m": e.get("billed_linear_m", 0)})
    return usage


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
