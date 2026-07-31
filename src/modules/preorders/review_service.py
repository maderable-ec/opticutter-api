"""Client review links over pre-orders: generation and confirmation.

The link and the client's decision live on the **pre-order** (mutable); the
immutable Order is only created upon confirmation. Link security (the token IS
the credential; the public endpoints have no other authentication):

- 256-bit CSPRNG token (``secrets.token_urlsafe(32)``); guessing it is
  infeasible, so it acts as a capability granting access to a single pre-order.
- Only its sha256 is stored at rest (a DB dump or a log leak yields nothing
  reusable). No salt: a random 256-bit token is its own salt.
- Unknown or revoked token → uniform 404 (it doesn't distinguish "doesn't exist"
  from "revoked", nor does it echo the token back), to avoid an enumeration oracle.
- A single active link per pre-order: regenerating revokes the previous one.
- Deployment notes: the token travels in the URL, the frontend must use
  ``Referrer-Policy: no-referrer``; rate limiting belongs on the reverse proxy.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.clients.service import require_phone
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.preorders.model import (
    OPEN_STATUSES,
    PreOrderModel,
    PreOrderReviewLinkModel,
    PreOrderStatus,
    ReviewLinkStatus,
)
from src.modules.preorders.service import PreOrderService
from src.shared.audit import Actor, client_actor, system_actor
from src.shared.config import config
from src.shared.database import get_db
from src.shared.exceptions import (
    AppError,
    BusinessRuleError,
    ConflictError,
    EntityNotFoundError,
)


class ReviewLinkNotFoundError(AppError):
    """Uniform 404: doesn't distinguish a nonexistent token from a revoked one."""

    status_code = 404
    code = "NOT_FOUND"

    def __init__(self):
        super().__init__("Enlace de revisión no válido")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PreOrderReviewService:
    """Manages the link lifecycle and the client's actions."""

    def __init__(self, db: Session):
        self.db = db
        self.preorders = PreOrderService(db)
        self.orders = OrderService(db)

    def generate(
        self,
        preorder_id: int,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> Tuple[PreOrderReviewLinkModel, str]:
        """Creates a new link (revoking the previous active one) and returns the token.

        Only for open pre-orders (``draft``/``sent``); requires the client to have
        a phone number (a quote isn't sent to someone who can't be invoiced).
        Transitions the pre-order to ``sent`` and refreshes its validity. Returns
        ``(link, raw_token)``; the token only ever exists in this response.
        """
        actor = actor or system_actor()
        preorder = self.preorders.get_scoped_or_404(preorder_id, branch_scope)
        if preorder.status not in {s.value for s in OPEN_STATUSES}:
            raise BusinessRuleError(
                "Solo se puede generar un enlace de revisión para una pre-orden "
                "abierta (no confirmada, rechazada ni vencida)."
            )
        require_phone(preorder.client)
        for previous in preorder.review_links:
            if previous.status == ReviewLinkStatus.active.value:
                previous.status = ReviewLinkStatus.revoked.value

        validity_days = self.preorders.settings_service.get_preorder_config()[
            "preorder_validity_days"
        ]
        now = datetime.utcnow()
        if preorder.status != PreOrderStatus.sent.value:
            self.preorders._record_transition(
                preorder,
                preorder.status,
                PreOrderStatus.sent,
                actor,
                note="Enlace de revisión enviado",
            )
        preorder.status = PreOrderStatus.sent.value
        preorder.sent_at = now
        preorder.expires_at = now + timedelta(days=validity_days)

        raw_token = secrets.token_urlsafe(32)
        link = PreOrderReviewLinkModel(
            preorder_id=preorder.id,
            token_hash=_hash(raw_token),
            status=ReviewLinkStatus.active.value,
            expires_at=preorder.expires_at,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link, raw_token

    def get_latest_info(
        self, preorder_id: int, branch_scope: Optional[int] = None
    ) -> PreOrderReviewLinkModel:
        """Latest link of the pre-order (metadata only; never exposes the token)."""
        preorder = self.preorders.get_scoped_or_404(preorder_id, branch_scope)
        if not preorder.review_links:
            raise EntityNotFoundError("ReviewLink de la pre-orden", preorder_id)
        return preorder.review_links[-1]

    def get_review(self, token: str) -> PreOrderModel:
        """Pre-order associated with the token for the public view (triggers expiry)."""
        link = self._get_by_token_or_404(token)
        return self.preorders.get_or_404(link.preorder_id)

    def confirm(
        self, token: str, note: Optional[str] = None, meta: Optional[dict] = None
    ) -> PreOrderModel:
        """The client confirms: mints the immutable Order and links ``order_id``.

        The Order is created from the current inputs (live catalog prices).
        Idempotent: if the pre-order is already confirmed, a retry returns its
        current state without error. The ``(client_id, hash)`` dedupe in
        ``OrderService.create`` prevents duplicate orders from a double click or
        a retry after a crash between the two commits.
        """
        link = self._get_by_token_or_404(token)
        preorder = self.preorders.get_or_404(link.preorder_id)
        status = PreOrderStatus(preorder.status)

        if status == PreOrderStatus.expired:
            raise BusinessRuleError("La cotización expiró; solicita una nueva.")
        if status in {PreOrderStatus.rejected, PreOrderStatus.cancelled}:
            raise BusinessRuleError("La cotización fue retirada; solicita una nueva.")
        if status == PreOrderStatus.confirmed:
            return preorder  # already confirmed: benign retry

        actor = client_actor()
        order = self.orders.create(
            OrderCreate(
                materials=preorder.materials,
                requirements=preorder.requirements,
                additional_services=preorder.additional_services,
                client_id=preorder.client_id,
                branch_id=preorder.branch_id,
                price_tier_code=preorder.price_tier_code,
                strategy=preorder.strategy,
                variant=preorder.variant or 0,
                notes=preorder.notes,
                source=preorder.source,
            ),
            actor=actor,
        )

        now = datetime.utcnow()
        self.preorders._record_transition(
            preorder, preorder.status, PreOrderStatus.confirmed, actor, note=note
        )
        preorder.status = PreOrderStatus.confirmed.value
        preorder.confirmed_at = now
        preorder.order_id = order.id
        self._mark_used(link, "confirmed", now, note, meta)
        self.db.commit()
        self.db.refresh(preorder)
        return preorder

    def reject(
        self, token: str, note: Optional[str] = None, meta: Optional[dict] = None
    ) -> PreOrderModel:
        """The client rejects the quote: pre-order ``sent → rejected``.

        Immediately frees up the open-pre-orders slot. If already confirmed, the
        contradictory rejection routes through sales (409): the order may already
        be in production.
        """
        link = self._get_by_token_or_404(token)
        preorder = self.preorders.get_or_404(link.preorder_id)
        status = PreOrderStatus(preorder.status)

        if status in {PreOrderStatus.rejected, PreOrderStatus.cancelled}:
            return preorder  # already withdrawn: benign retry
        if status == PreOrderStatus.expired:
            raise BusinessRuleError("La cotización expiró; no hay nada que rechazar.")
        if status == PreOrderStatus.confirmed:
            raise ConflictError(
                "La cotización ya fue confirmada; para anularla contacta a ventas."
            )

        now = datetime.utcnow()
        self.preorders._record_transition(
            preorder,
            preorder.status,
            PreOrderStatus.rejected,
            client_actor(),
            note=note,
        )
        preorder.status = PreOrderStatus.rejected.value
        self._mark_used(link, "rejected", now, note, meta)
        self.db.commit()
        self.db.refresh(preorder)
        return preorder

    def request_changes(self, token: str, note: Optional[str] = None) -> PreOrderModel:
        """The client requests an adjustment: pre-order ``sent → changes_requested``.

        Neither discards it (unlike ``reject``) nor commits to it: the ball goes
        back to the workshop, which will edit the pre-order. **Doesn't consume the
        link** (it stays active): the client will use the same token to see the
        edited version and then confirm or reject. The note (what to change) is
        stored in ``client_note``.
        """
        link = self._get_by_token_or_404(token)
        preorder = self.preorders.get_or_404(link.preorder_id)
        status = PreOrderStatus(preorder.status)

        if status == PreOrderStatus.expired:
            raise BusinessRuleError("La cotización expiró; solicita una nueva.")
        if status in {PreOrderStatus.rejected, PreOrderStatus.cancelled}:
            raise BusinessRuleError("La cotización fue retirada; solicita una nueva.")
        if status == PreOrderStatus.confirmed:
            raise ConflictError(
                "La cotización ya fue confirmada; para cambios contacta a ventas."
            )

        # 'sent' or 'changes_requested': records/updates the request and keeps
        # the link active (without marking it used).
        if preorder.status != PreOrderStatus.changes_requested.value:
            self.preorders._record_transition(
                preorder,
                preorder.status,
                PreOrderStatus.changes_requested,
                client_actor(),
                note=note,
            )
        preorder.status = PreOrderStatus.changes_requested.value
        preorder.client_note = note
        self.db.commit()
        self.db.refresh(preorder)
        return preorder

    def build_url(self, raw_token: str) -> str:
        """Full review URL the client opens (Maderable frontend)."""
        return f"{config.FRONTEND_BASE_URL.rstrip('/')}/review/{raw_token}"

    def _mark_used(
        self,
        link: PreOrderReviewLinkModel,
        action: str,
        now: datetime,
        note: Optional[str],
        meta: Optional[dict],
    ) -> None:
        link.status = ReviewLinkStatus.used.value
        link.used_at = now
        link.used_meta = {"action": action, "note": note, **(meta or {})}

    def _get_by_token_or_404(self, token: str) -> PreOrderReviewLinkModel:
        """Looks up by token hash; uniform 404 if it doesn't exist or was revoked.

        A ``used`` link remains readable: the client can return to the page after
        confirming and see the actual state of their quote.
        """
        link = (
            self.db.query(PreOrderReviewLinkModel)
            .filter(PreOrderReviewLinkModel.token_hash == _hash(token))
            .first()
        )
        if link is None or link.status == ReviewLinkStatus.revoked.value:
            raise ReviewLinkNotFoundError()
        return link


def preorder_review_service(db: Session = Depends(get_db)) -> PreOrderReviewService:
    """Provider for ``PreOrderReviewService`` injection in routes."""
    return PreOrderReviewService(db)
