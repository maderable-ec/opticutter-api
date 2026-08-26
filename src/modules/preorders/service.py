from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.branches.service import resolve_branch_for_create
from src.modules.clients.model import ClientModel
from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.pricing import build_pricing
from src.modules.optimizations.schemas import OptimizeRequest, OptimizeResponse
from src.modules.optimizations.service import OptimizationService
from src.modules.preorders.model import (
    OPEN_STATUSES,
    PreOrderModel,
    PreOrderStatus,
    PreOrderStatusHistoryModel,
)
from src.modules.preorders.schemas import PreOrderCreate, PreOrderUpdate
from src.modules.settings.service import SettingsService
from src.shared.audit import Actor, system_actor
from src.shared.branch_scope import BranchScopedMixin
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError

_OPEN_VALUES = [s.value for s in OPEN_STATUSES]


class PreOrderService(BranchScopedMixin):
    """Manages pre-orders: mutable CRUD, cache-first recompute and anti-abuse.

    Freezes nothing: stores the inputs (``materials`` + ``requirements``) and
    delegates the computation to ``OptimizationService.compute`` (cache-first)
    every time the quote or the PDF needs to be shown. The immutable Order is
    minted separately, when the client confirms (see ``PreOrderReviewService``).

    Branch-scoped (``BranchScopedMixin``): staff only sees/edits the ones in their
    branch; the admin (scope ``None``) sees all of them.
    """

    model = PreOrderModel

    def __init__(self, db: Session):
        self.db = db
        self.optimization_service = OptimizationService(db)
        self.settings_service = SettingsService(db)

    def get_or_404(self, preorder_id: int) -> PreOrderModel:
        preorder = self.db.get(PreOrderModel, preorder_id)
        if preorder is None:
            raise EntityNotFoundError("PreOrder", preorder_id)
        if self._expire_if_stale(preorder):
            self.db.commit()
            self.db.refresh(preorder)
        return preorder

    def list_preorders(
        self,
        status: Optional[List[PreOrderStatus]] = None,
        client_id: Optional[int] = None,
        branch_scope: Optional[int] = None,
        branch_filter: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
        sort: str = "recent",
    ) -> Tuple[List[PreOrderModel], int]:
        """Lists pre-orders with a count: ``(items, total)``.

        ``status`` filters by one or more statuses (empty list/``None`` = all).
        ``branch_scope`` confines staff to their branch; the admin (``None``) sees
        all of them and can narrow with ``branch_filter``.

        ``search`` matches the quote code or the client (identifier/first/last
        name), and also the pre-order id when the term is all digits -- the same
        contract the orders listing offers.

        ``sort`` defaults to ``recent``: unlike orders, nothing reads this
        listing FIFO, and newest-first is what it has always returned.
        """
        self._sweep_expired()
        query = self.db.query(PreOrderModel)
        if status:
            query = query.filter(PreOrderModel.status.in_([s.value for s in status]))
        if client_id is not None:
            query = query.filter(PreOrderModel.client_id == client_id)
        if search:
            pattern = f"%{search}%"
            # Outer join: a pre-order always has a client, but the join must not
            # silently drop rows if that ever stops holding.
            query = query.outerjoin(
                ClientModel, PreOrderModel.client_id == ClientModel.id
            )
            term = (
                PreOrderModel.code.ilike(pattern)
                | ClientModel.identifier.ilike(pattern)
                | ClientModel.first_name.ilike(pattern)
                | ClientModel.last_name.ilike(pattern)
            )
            if search.strip().isdigit():
                term = term | (PreOrderModel.id == int(search.strip()))
            query = query.filter(term)
        # ``created_at`` is UTC-naive (TimestampMixin), so the day boundaries are
        # UTC ones. ``created_to`` is inclusive: compare against the next midnight.
        if created_from is not None:
            query = query.filter(
                PreOrderModel.created_at >= datetime.combine(created_from, time.min)
            )
        if created_to is not None:
            query = query.filter(
                PreOrderModel.created_at
                < datetime.combine(created_to + timedelta(days=1), time.min)
            )
        query = self._apply_branch_scope(query, branch_scope, branch_filter)
        total = query.count()
        order_by = (
            PreOrderModel.id.asc() if sort == "oldest" else PreOrderModel.id.desc()
        )
        items = query.order_by(order_by).offset(offset).limit(limit).all()
        return items, total

    def create(
        self,
        data: PreOrderCreate,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
        default_branch_id: Optional[int] = None,
    ) -> PreOrderModel:
        """Creates an open (``draft``) pre-order with the optimizer inputs.

        The branch is resolved from ``branch_scope``: the operator is pinned to
        their own; global roles (admin/seller) use ``data.branch_id`` if given,
        otherwise ``default_branch_id`` (the creator's base branch — the seller
        defaults to one, the admin has none and must provide ``branchId``). The
        anti-abuse cap is per branch.
        """
        actor = actor or system_actor()
        if self.db.get(ClientModel, data.client_id) is None:
            raise EntityNotFoundError("Client", data.client_id)
        branch_id = resolve_branch_for_create(
            self.db, branch_scope, data.branch_id, default_branch_id
        )
        self._enforce_open_cap(data.client_id, branch_id)
        # Validates the price tier (422 if missing/inactive) and normalizes it.
        tier = self.settings_service.resolve_price_tier(data.price_tier_code)

        validity_days = self.settings_service.get_preorder_config()[
            "preorder_validity_days"
        ]
        now = datetime.utcnow()
        preorder = PreOrderModel(
            client_id=data.client_id,
            branch_id=branch_id,
            status=PreOrderStatus.draft.value,
            materials=[m.model_dump(mode="json") for m in data.materials],
            requirements=[r.model_dump(mode="json") for r in data.requirements],
            additional_services=[
                s.model_dump(mode="json") for s in data.additional_services
            ],
            price_tier_code=tier["code"],
            strategy=data.strategy.value,
            variant=data.variant,
            source=data.source,
            notes=data.notes,
            created_at=now,
            expires_at=now + timedelta(days=validity_days),
            created_by=actor.user_id,
        )
        self._record_transition(
            preorder, None, PreOrderStatus.draft, actor, note="Pre-orden creada"
        )
        self.db.add(preorder)
        self.db.flush()  # assigns the id needed to build the readable code
        preorder.code = f"PRE-{now.year}-{preorder.id:04d}"
        self.db.commit()
        self.db.refresh(preorder)
        return preorder

    def update(
        self,
        preorder_id: int,
        data: PreOrderUpdate,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> PreOrderModel:
        """Edits an open pre-order; rejects it if already terminal (confirmed/etc.)."""
        actor = actor or system_actor()
        preorder = self.get_scoped_or_404(preorder_id, branch_scope)
        self._ensure_open(preorder)
        fields = data.model_dump(exclude_unset=True)
        if data.client_id is not None:
            if self.db.get(ClientModel, data.client_id) is None:
                raise EntityNotFoundError("Client", data.client_id)
            preorder.client_id = data.client_id
        if data.materials is not None:
            preorder.materials = [m.model_dump(mode="json") for m in data.materials]
        if data.requirements is not None:
            preorder.requirements = [
                r.model_dump(mode="json") for r in data.requirements
            ]
        if data.additional_services is not None:
            preorder.additional_services = [
                s.model_dump(mode="json") for s in data.additional_services
            ]
        if data.price_tier_code is not None:
            tier = self.settings_service.resolve_price_tier(data.price_tier_code)
            preorder.price_tier_code = tier["code"]
        if data.strategy is not None:
            preorder.strategy = data.strategy.value
        if data.variant is not None:
            preorder.variant = data.variant
        if "notes" in fields:
            preorder.notes = data.notes
        if "source" in fields:
            preorder.source = data.source
        # If the client had requested changes, editing = "addressed": the pre-order
        # goes back to 'sent' (the ball returns to the client) and the request is cleared.
        if preorder.status == PreOrderStatus.changes_requested.value:
            self._record_transition(
                preorder,
                preorder.status,
                PreOrderStatus.sent,
                actor,
                note="Cambios atendidos; reenviada al cliente",
            )
            preorder.status = PreOrderStatus.sent.value
            preorder.client_note = None
        preorder.updated_by = actor.user_id
        self.db.commit()
        self.db.refresh(preorder)
        return preorder

    def delete(self, preorder_id: int, branch_scope: Optional[int] = None) -> None:
        """Deletes a pre-order (unless already confirmed: it has a live order)."""
        preorder = self.get_scoped_or_404(preorder_id, branch_scope)
        if preorder.status == PreOrderStatus.confirmed.value:
            raise BusinessRuleError(
                "No se puede eliminar una pre-orden ya confirmada; la orden generada "
                "es la fuente de verdad."
            )
        self.db.delete(preorder)
        self.db.commit()

    def build_request(self, preorder: PreOrderModel) -> OptimizeRequest:
        """Rebuilds the ``OptimizeRequest`` from the stored inputs.

        Carries the price tier so ``optimize_response`` attaches the ``pricing``
        block (it doesn't affect geometry or the hash) and the stored ``strategy``
        to reproduce the same layout (this one does affect geometry and the hash).
        """
        return OptimizeRequest(
            materials=preorder.materials,
            requirements=preorder.requirements,
            client_id=preorder.client_id,
            price_tier_code=preorder.price_tier_code,
            strategy=preorder.strategy,
            variant=preorder.variant or 0,
        )

    def compute_payload(self, preorder: PreOrderModel) -> Tuple[dict, str]:
        """Optimizer payload (cache-first) for the pre-order."""
        return self.optimization_service.compute(self.build_request(preorder))

    def build_pricing_for(self, preorder: PreOrderModel, payload: dict) -> dict:
        """Live discount block for the pre-order's price tier (incl. services).

        The discounted boards are read back through ``build_request``: the stored
        ``materials`` are raw JSON, and validating them through the same union the
        re-optimization already uses is what keeps ``applyDiscount`` from being
        parsed by hand here.
        """
        tier = self.settings_service.resolve_price_tier(preorder.price_tier_code)
        return build_pricing(
            payload,
            tier,
            preorder.additional_services,
            self.build_request(preorder).discounted_material_keys,
        )

    def build_optimize_response(self, preorder: PreOrderModel) -> OptimizeResponse:
        """Optimization response (with client) for the internal detail view.

        Threads the stored services so ``optimization.pricing`` already reflects
        the services-inclusive total.
        """
        return self.optimization_service.optimize_response(
            self.build_request(preorder),
            additional_services=preorder.additional_services,
        )

    def build_carrier(self, preorder: PreOrderModel) -> ProformaCarrier:
        """Recomputed proforma carrier (PDF) for the pre-order (quote)."""
        payload, _ = self.compute_payload(preorder)
        payload = {
            **payload,
            "pricing": self.build_pricing_for(preorder, payload),
            "additional_services": preorder.additional_services,
        }
        return ProformaCarrier.from_payload(
            payload,
            preorder.client,
            reference=preorder.code or f"PRE-{preorder.id:06d}",
            company=self.settings_service.get_company(),
            validity_days=self.settings_service.get_preorder_config()[
                "preorder_validity_days"
            ],
            notes=preorder.notes,
        )

    def _record_transition(
        self,
        preorder: PreOrderModel,
        from_status: Optional[str],
        to_status: PreOrderStatus,
        actor: Actor,
        note: Optional[str] = None,
    ) -> None:
        """Appends a transition history entry (the caller persists it)."""
        preorder.history.append(
            PreOrderStatusHistoryModel(
                from_status=from_status,
                to_status=to_status.value,
                actor=actor.type,
                actor_user_id=actor.user_id,
                actor_label=actor.label,
                note=note,
            )
        )

    def _ensure_open(self, preorder: PreOrderModel) -> None:
        if preorder.status not in _OPEN_VALUES:
            raise BusinessRuleError(
                f"La pre-orden está en estado '{preorder.status}' y ya no puede "
                "editarse."
            )

    def _sweep_expired(self) -> None:
        """Expires (and persists) any open ones past their validity before count/page."""
        stale = (
            self.db.query(PreOrderModel)
            .filter(
                PreOrderModel.status.in_(_OPEN_VALUES),
                PreOrderModel.expires_at < datetime.utcnow(),
            )
            .all()
        )
        if any([self._expire_if_stale(p) for p in stale]):
            self.db.commit()

    def _expire_if_stale(self, preorder: PreOrderModel) -> bool:
        """Marks an open pre-order as ``expired`` once its validity period is over."""
        if (
            preorder.expires_at is not None
            and preorder.status in _OPEN_VALUES
            and preorder.expires_at < datetime.utcnow()
        ):
            self._record_transition(
                preorder,
                preorder.status,
                PreOrderStatus.expired,
                system_actor(),
                note="Vigencia vencida",
            )
            preorder.status = PreOrderStatus.expired.value
            return True
        return False

    def _enforce_open_cap(self, client_id: int, branch_id: int) -> None:
        """Blocks if the client exceeds the open pre-orders cap for the branch.

        The cap is counted per ``(branch, client)``: the same client can have open
        quotes in different branches without them interfering with each other.
        """
        candidates = (
            self.db.query(PreOrderModel)
            .filter(
                PreOrderModel.client_id == client_id,
                PreOrderModel.branch_id == branch_id,
                PreOrderModel.status.in_(_OPEN_VALUES),
            )
            .all()
        )
        if any([self._expire_if_stale(p) for p in candidates]):
            self.db.commit()
        active = sum(1 for p in candidates if p.status in _OPEN_VALUES)
        cap = self.settings_service.get_preorder_config()[
            "max_open_preorders_per_client"
        ]
        if active >= cap:
            raise BusinessRuleError(
                f"El cliente ya tiene {active} pre-orden(es) abierta(s); "
                "ciérrelas o espere a que expiren antes de crear otra."
            )


def preorder_service(db: Session = Depends(get_db)) -> PreOrderService:
    """Provider for ``PreOrderService`` injection in routes."""
    return PreOrderService(db)
