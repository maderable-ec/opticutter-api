"""Emits notifications as a side-effect of order EVENTS.

Lives in the notifications module so ``orders`` stays ignorant of who gets
notified -- and of when an event is worth announcing: it hands over the order
plus what happened, and this resolves recipients by role/branch and fans out one
row per recipient. Best-effort throughout -- a failure here never breaks an
operation that already committed (same philosophy as the cache: "accelerator,
not source of truth").

Three events, one per public function. A state transition is only one of them:
an order being born has no ``from_status`` and a branch change has
``from == to``, so neither can be expressed as a transition. That is why
``resolve_plan`` (the transition map) is no longer the entry point -- it is one
of three, and they all share ``_emit``.

It reads attributes off the order (``id``/``code``/``branch_id``) instead of
importing the orders package, so there is no import cycle
(``orders -> notifications``); only the ``OrderStatus`` enum is imported. The
same rule is why the pre-order and the branches arrive as plain scalars: this
module knows neither package.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from src.modules.notifications.enums import NotificationType
from src.modules.notifications.service import NotificationService
from src.modules.orders.model import OrderStatus
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.modules.users.service import UserService
from src.shared.audit import Actor

logger = logging.getLogger(__name__)

#: Resolves the recipients of one notification. A callable rather than a ready
#: list so the query runs INSIDE ``_emit``'s try/except: every emission happens
#: after a commit, where a DB failure must not propagate back to the caller.
RecipientResolver = Callable[[Session], List[UserModel]]

#: ``(order code, payload) -> (title, body)``.
Renderer = Callable[[str, Mapping[str, Any]], Tuple[str, str]]


class _Audience(Enum):
    """Who receives a given transition's notification."""

    GLOBAL_ADMINS_SELLERS = "global_admins_sellers"
    BRANCH_OPERATORS = "branch_operators"
    # Admins ALONE, not the office: a cancellation is a sale that died, which is a
    # management fact rather than a sales one. The seller who raised it is either
    # the actor (already excluded) or is told through the order itself.
    GLOBAL_ADMINS = "global_admins"


@dataclass(frozen=True)
class NotificationPlan:
    """What to emit for a transition: the event type and its audience."""

    type: NotificationType
    audience: _Audience


def resolve_plan(
    from_status: OrderStatus, to_status: OrderStatus
) -> List[NotificationPlan]:
    """Maps a transition to its notification plans (empty if it isn't notified).

    Pure and DB-free (unit-testable): ``-> finished`` notifies the global
    admins/sellers and the real enqueue ``confirmed -> queued`` the branch
    operators. The admin rollback ``in_process -> queued`` and every other
    transition produce nothing.

    ``-> finished`` normally arrives DERIVED from the last activity closing, so
    this is what tells the office the work is done without anybody pressing a
    button for it.

    A LIST and not one plan, because cancelling concerns two disjoint audiences
    at once -- which is the same thing that forced the branch change out of this
    function into ``notify_order_branch_changed``. Here it fits, because a
    cancellation IS a transition.
    """
    if to_status == OrderStatus.finished:
        return [
            NotificationPlan(
                NotificationType.order_completed, _Audience.GLOBAL_ADMINS_SELLERS
            )
        ]
    if to_status == OrderStatus.queued and from_status == OrderStatus.confirmed:
        return [
            NotificationPlan(NotificationType.order_queued, _Audience.BRANCH_OPERATORS)
        ]
    if to_status == OrderStatus.cancelled:
        # The admins hear about EVERY cancellation: an order only exists because a
        # client confirmed a quote, so killing one is a sale that died either way.
        plans = [
            NotificationPlan(NotificationType.order_cancelled, _Audience.GLOBAL_ADMINS)
        ]
        # The shop floor only hears about the ones that were in its queue: an order
        # cancelled while ``confirmed`` was never on anybody's board. The two get the
        # same type because the fact is the same one -- the copy, which reads the
        # payload's ``fromStatus``, is what says whether a card just vanished.
        if from_status == OrderStatus.queued:
            plans.append(
                NotificationPlan(
                    NotificationType.order_cancelled, _Audience.BRANCH_OPERATORS
                )
            )
        return plans
    return []


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #
def _global_admins_sellers(db: Session) -> List[UserModel]:
    """Every active administrator and seller (the office)."""
    return UserService(db).list_by_roles([UserRole.ADMIN, UserRole.SELLER])


def _branch_operators(db: Session, branch_id: Optional[int]) -> List[UserModel]:
    """Active operators of an EXPLICIT branch.

    Explicit and not ``order.branch_id``, because a branch change has to reach
    the branch the order just LEFT, which by then is no longer the order's.
    """
    if branch_id is None:
        return []
    return UserService(db).list_by_role_and_branch(UserRole.OPERATOR, branch_id)


def _quote_owner_or_global(db: Session, user_id: Optional[int]) -> List[UserModel]:
    """The user who raised the quote; the office if they are no longer around.

    A confirmation is a closed sale, so it must not be lost to a seller who was
    deleted or deactivated between the quote and the client's answer.
    """
    if user_id is not None:
        owner = db.get(UserModel, user_id)
        if owner is not None and owner.is_active:
            return [owner]
    return _global_admins_sellers(db)


def _global_admins(db: Session) -> List[UserModel]:
    """Every active administrator, without the sellers."""
    return UserService(db).list_by_roles([UserRole.ADMIN])


def _recipients(db: Session, audience: _Audience, order) -> List[UserModel]:
    """Adapter for ``resolve_plan``'s audiences."""
    if audience is _Audience.GLOBAL_ADMINS_SELLERS:
        return _global_admins_sellers(db)
    if audience is _Audience.GLOBAL_ADMINS:
        return _global_admins(db)
    return _branch_operators(db, order.branch_id)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
# One entry per ``NotificationType``, enforced by a unit test. It used to be an
# if/else whose ``else`` was the "queued" copy, so a new type silently shipped
# the wrong message. Sizes are bounded by the columns (title 128, body 255) and
# a branch name can be 128 on its own -- hence a single branch name per body.
def _render_completed(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    return (
        f"Orden {code} terminada",
        f"La orden {code} terminó todo su trabajo de taller.",
    )


def _render_queued(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    return (
        f"Orden {code} en cola",
        f"La orden {code} entró a la cola de producción.",
    )


def _render_confirmed(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    quote = payload.get("preorderCode")
    detail = f"la cotización {quote}" if quote else "su cotización"
    return (
        f"Orden {code} confirmada",
        f"El cliente confirmó {detail} y se generó la orden {code}.",
    )


def _render_branch_arrived(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    origin = payload.get("fromBranch") or "otra sucursal"
    return (
        f"Orden {code} asignada a tu sucursal",
        f"La orden {code} llegó a tu cola desde {origin}.",
    )


def _render_branch_left(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    destination = payload.get("toBranch") or "otra sucursal"
    return (
        f"Orden {code} movida a otra sucursal",
        f"La orden {code} salió de tu cola hacia {destination}.",
    )


def _render_cancelled(code: str, payload: Mapping[str, Any]) -> Tuple[str, str]:
    # One type, two situations: from the queue a card just vanished off the board
    # (and the operators are among the recipients), from ``confirmed`` the order
    # never reached the shop and only the office is being told.
    left_the_queue = payload.get("fromStatus") == OrderStatus.queued.value
    tail = " y salió de la cola de producción" if left_the_queue else ""
    return (
        f"Orden {code} cancelada",
        f"La orden {code} fue cancelada{tail}.",
    )


_RENDERERS: Dict[NotificationType, Renderer] = {
    NotificationType.order_completed: _render_completed,
    NotificationType.order_queued: _render_queued,
    NotificationType.order_confirmed: _render_confirmed,
    NotificationType.order_branch_arrived: _render_branch_arrived,
    NotificationType.order_branch_left: _render_branch_left,
    NotificationType.order_cancelled: _render_cancelled,
}


def _render(
    notification_type: NotificationType, order, payload: Mapping[str, Any]
) -> Tuple[str, str]:
    """Human title + body (Spanish, uses the order code)."""
    code = order.code or f"#{order.id}"
    return _RENDERERS[notification_type](code, payload)


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def _emit(
    db: Session,
    order,
    notification_type: NotificationType,
    resolve_recipients: RecipientResolver,
    actor: Optional[Actor] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Resolves recipients, drops the actor, renders and fans out.

    The single try/except of the module: every event goes through here, so none
    of them can forget to be best-effort. Reading ``order.code`` is inside it on
    purpose -- after the caller's commit the instance is expired, so that access
    is itself a query.

    ``data`` doubles as the render context and as the row's stored payload: what
    the copy needs (the quote's code, a branch name) is exactly what the
    dashboard wants next to the deep link.
    """
    try:
        actor_id = actor.user_id if actor else None
        user_ids = [user.id for user in resolve_recipients(db) if user.id != actor_id]
        if not user_ids:
            return
        payload: Dict[str, Any] = {"orderCode": order.code, **(data or {})}
        title, body = _render(notification_type, order, payload)
        NotificationService(db).create_bulk(
            user_ids=user_ids,
            notification_type=notification_type,
            title=title,
            body=body,
            order_id=order.id,
            data=payload,
        )
    except Exception:
        # Best-effort: notifications must never break a committed operation.
        logger.exception(
            "Failed to emit %s for order %s",
            notification_type.value,
            getattr(order, "id", "?"),
        )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def notify_order_transition(
    db: Session,
    order,
    from_status: OrderStatus,
    to_status: OrderStatus,
    actor: Optional[Actor] = None,
) -> None:
    """Fan-out for a just-committed status transition (best-effort).

    One emission per plan, and they are independent on purpose: the audiences are
    disjoint (a user holds one role), and if the second fan-out fails the first
    still stands -- the same trade the branch change makes.

    ``fromStatus`` rides in the payload because the copy of a cancellation turns
    on it: the same event reads differently depending on whether the order was
    already in the shop's queue.
    """
    data = {"status": to_status.value, "fromStatus": from_status.value}
    for plan in resolve_plan(from_status, to_status):
        _emit(
            db,
            order,
            plan.type,
            partial(_recipients, audience=plan.audience, order=order),
            actor=actor,
            data=data,
        )


def notify_order_confirmed(
    db: Session,
    order,
    preorder_code: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    actor: Optional[Actor] = None,
) -> None:
    """The client accepted the quote: tells whoever raised it (best-effort).

    The actor here is the client (no ``user_id``), so nobody is excluded -- the
    seller must hear about it precisely because someone else triggered it. The
    exclusion still applies in general, and that is deliberate: a staff member
    is never notified of their own action, even if that leaves the fan-out empty
    (it does NOT fall back to the office in that case).

    One accepted duplicate: two distinct quotes of the same client, branch, hash
    and totals resolve to ONE order (``OrderService.create`` dedupes), so
    confirming the second emits a second notification against the same
    ``order_id``. That is correct -- the second quote's owner closed a sale too
    -- and ``data["preorderCode"]`` tells the two apart.
    """
    _emit(
        db,
        order,
        NotificationType.order_confirmed,
        partial(_quote_owner_or_global, user_id=owner_user_id),
        actor=actor,
        data={
            "preorderCode": preorder_code,
            "status": OrderStatus.confirmed.value,
        },
    )


def notify_order_branch_changed(
    db: Session,
    order,
    order_status: OrderStatus,
    from_branch_id: int,
    to_branch_id: int,
    from_branch_name: Optional[str] = None,
    to_branch_name: Optional[str] = None,
    actor: Optional[Actor] = None,
) -> None:
    """Tells both shop floors the order moved (best-effort).

    Only for a ``queued`` order, and that is the whole policy: a ``confirmed``
    one was never on the origin's board (which lists ``queued``/``in_process``)
    and cannot be taken at the destination until it is paid for -- at which
    point the destination gets its own ``order.queued``. Notifying there would
    be noise about work nobody can start.

    The branch NAMES arrive already resolved, as plain strings: this module does
    not know the branches package, and by the time it runs the caller has
    committed, so the origin is no longer reachable through the order.

    Two fan-outs, two commits. They are independent by design -- if the second
    fails the first still stands, which beats losing both halves of a move that
    already happened.
    """
    if order_status is not OrderStatus.queued:
        return
    common = {
        "fromBranchId": from_branch_id,
        "toBranchId": to_branch_id,
        "fromBranch": from_branch_name,
        "toBranch": to_branch_name,
        "status": order_status.value,
    }
    _emit(
        db,
        order,
        NotificationType.order_branch_arrived,
        partial(_branch_operators, branch_id=to_branch_id),
        actor=actor,
        data=common,
    )
    _emit(
        db,
        order,
        NotificationType.order_branch_left,
        partial(_branch_operators, branch_id=from_branch_id),
        actor=actor,
        data=common,
    )
