"""Emits notifications as a side-effect of order status transitions.

Lives in the notifications module so ``orders`` stays ignorant of who gets
notified: it hands over the order + transition and this resolves recipients by
role/branch and fans out one row per recipient. Best-effort — a failure here
never breaks a transition that already committed (same philosophy as the cache:
"accelerator, not source of truth").

It reads attributes off the order (``id``/``code``/``branch_id``) instead of
importing the orders package, so there is no import cycle
(``orders -> notifications``); only the ``OrderStatus`` enum is imported.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.modules.notifications.enums import NotificationType
from src.modules.notifications.service import NotificationService
from src.modules.orders.model import OrderStatus
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.modules.users.service import UserService
from src.shared.audit import Actor

logger = logging.getLogger(__name__)


class _Audience(Enum):
    """Who receives a given notification type."""

    GLOBAL_ADMINS_SELLERS = "global_admins_sellers"
    BRANCH_OPERATORS = "branch_operators"


@dataclass(frozen=True)
class NotificationPlan:
    """What to emit for a transition: the event type and its audience."""

    type: NotificationType
    audience: _Audience


def resolve_plan(
    from_status: OrderStatus, to_status: OrderStatus
) -> Optional[NotificationPlan]:
    """Maps a transition to a notification plan (``None`` if it isn't notified).

    Pure and DB-free (unit-testable): ``-> finished`` notifies the global
    admins/sellers; the real enqueue ``confirmed -> queued`` notifies the branch
    operators. The admin rollback ``in_process -> queued`` and every other
    transition produce nothing.

    ``-> finished`` normally arrives DERIVED from the last activity closing, so
    this is what tells the office the work is done without anybody pressing a
    button for it.
    """
    if to_status == OrderStatus.finished:
        return NotificationPlan(
            NotificationType.order_completed, _Audience.GLOBAL_ADMINS_SELLERS
        )
    if to_status == OrderStatus.queued and from_status == OrderStatus.confirmed:
        return NotificationPlan(
            NotificationType.order_queued, _Audience.BRANCH_OPERATORS
        )
    return None


def _recipients(db: Session, audience: _Audience, order) -> List[UserModel]:
    """Active users to notify for the given audience."""
    users = UserService(db)
    if audience is _Audience.GLOBAL_ADMINS_SELLERS:
        return users.list_by_roles([UserRole.ADMIN, UserRole.SELLER])
    return users.list_by_role_and_branch(UserRole.OPERATOR, order.branch_id)


def _render(notification_type: NotificationType, order) -> Tuple[str, str]:
    """Human title + body for the notification (Spanish, uses the order code)."""
    code = order.code or f"#{order.id}"
    if notification_type is NotificationType.order_completed:
        return (
            f"Orden {code} terminada",
            f"La orden {code} terminó todo su trabajo de taller.",
        )
    return (
        f"Orden {code} en cola",
        f"La orden {code} entró a la cola de producción.",
    )


def notify_order_transition(
    db: Session,
    order,
    from_status: OrderStatus,
    to_status: OrderStatus,
    actor: Optional[Actor] = None,
) -> None:
    """Fan-out notifications for a just-committed transition (best-effort).

    Resolves the plan, excludes the acting user (they triggered it), and creates
    one notification per remaining recipient. Any failure is swallowed and logged
    so it never propagates back into the already-committed transition.
    """
    try:
        plan = resolve_plan(from_status, to_status)
        if plan is None:
            return
        actor_id = actor.user_id if actor else None
        user_ids = [
            user.id
            for user in _recipients(db, plan.audience, order)
            if user.id != actor_id
        ]
        if not user_ids:
            return
        title, body = _render(plan.type, order)
        NotificationService(db).create_bulk(
            user_ids=user_ids,
            notification_type=plan.type,
            title=title,
            body=body,
            order_id=order.id,
            data={"orderCode": order.code, "status": to_status.value},
        )
    except Exception:
        # Best-effort: notifications must never break a committed transition.
        logger.exception(
            "Failed to emit notifications for order %s",
            getattr(order, "id", "?"),
        )
