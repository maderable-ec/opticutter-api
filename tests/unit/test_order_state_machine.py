"""Unit: ``OrderService.transition`` state machine (no DB).

Builds a transient ``OrderModel`` (no session) and replaces the id lookup
(``get_scoped_or_404``) with the object in hand, so the transition gates are
exercised without touching PostgreSQL. This is the reference pattern for testing
service logic with ``mock_session``: every path that rejects the transition must
**not** call ``commit``.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from src.modules.orders.model import (
    ActivityStatus,
    ActivityType,
    OrderActivityModel,
    OrderModel,
    OrderStatus,
)
from src.modules.orders.schemas import OrderPaymentInput
from src.modules.orders.service import OrderService, _has_payment, _progress
from src.modules.users.enums import UserRole
from src.shared.audit import Actor
from src.shared.exceptions import (
    AuthorizationError,
    BusinessRuleError,
    ValidationError,
)


def _order(status: OrderStatus, *activities: OrderActivityModel) -> OrderModel:
    """Transient order (no session) in the requested status.

    ``activities`` are the rows the closing gate reads; the default is an order
    whose work is all done, so a test that is not about the gate does not have to
    describe one.
    """
    order = OrderModel(status=status.value)
    order.id = 1
    order.activities = list(activities) or [_activity(ActivityType.cutting)]
    return order


def _activity(
    activity_type: ActivityType, status: ActivityStatus = ActivityStatus.done
) -> OrderActivityModel:
    return OrderActivityModel(type=activity_type.value, status=status.value)


def _service(mock_session, order: OrderModel) -> OrderService:
    svc = OrderService(mock_session)
    # The branch-scoped id lookup is replaced with the order in hand.
    svc.get_scoped_or_404 = lambda *a, **k: order
    svc._ensure_activities = lambda *a, **k: None
    return svc


def _actor(role: UserRole | None) -> Actor:
    return Actor("staff", user_id=1, label="Tester", role=role.value if role else None)


# --- Invalid transitions / authorization ---------------------------------------
def test_invalid_transition_raises_and_does_not_commit(mock_session):
    svc = _service(mock_session, _order(OrderStatus.confirmed))
    with pytest.raises(BusinessRuleError):
        svc.transition(1, OrderStatus.in_process, actor=_actor(UserRole.ADMIN))
    mock_session.commit.assert_not_called()


def test_role_gate_blocks_unauthorized_role(mock_session):
    svc = _service(mock_session, _order(OrderStatus.confirmed))
    # confirmed -> queued can only be done by admin/seller, not the operator.
    with pytest.raises(AuthorizationError):
        svc.transition(1, OrderStatus.queued, actor=_actor(UserRole.OPERATOR))
    mock_session.commit.assert_not_called()


# --- Payment-method gate (confirmed -> queued) ----------------------------------
def test_queued_requires_payment(mock_session):
    svc = _service(mock_session, _order(OrderStatus.confirmed))
    with pytest.raises(ValidationError):
        svc.transition(
            1, OrderStatus.queued, actor=_actor(UserRole.ADMIN), payment=None
        )
    mock_session.commit.assert_not_called()


def test_queued_with_payment_freezes_amount_and_commits(mock_session):
    order = _order(OrderStatus.confirmed)
    svc = _service(mock_session, order)
    svc.transition(
        1,
        OrderStatus.queued,
        actor=_actor(UserRole.ADMIN),
        payment=OrderPaymentInput(cash_amount=50.0),
    )
    assert order.status == OrderStatus.queued.value
    assert order.payment_cash_amount == 50.0
    mock_session.commit.assert_called_once()


def test_queued_freezes_every_method_split(mock_session):
    """The three amounts are frozen together: one transition, one payment."""
    order = _order(OrderStatus.confirmed)
    svc = _service(mock_session, order)
    svc.transition(
        1,
        OrderStatus.queued,
        actor=_actor(UserRole.ADMIN),
        payment=OrderPaymentInput(
            cash_amount=10.0, transfer_amount=20.0, credit_amount=5.0
        ),
    )
    assert order.payment_cash_amount == 10.0
    assert order.payment_transfer_amount == 20.0
    assert order.payment_credit_amount == 5.0


# --- Closing gate: the work has to be finished -----------------------------------
def test_finished_blocked_while_an_activity_is_open(mock_session):
    """Generalizes the old banding-only gate to the three activities."""
    order = _order(
        OrderStatus.in_process,
        _activity(ActivityType.cutting),
        _activity(ActivityType.banding, ActivityStatus.in_progress),
    )
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError) as exc:
        svc.transition(1, OrderStatus.finished, actor=_actor(UserRole.ADMIN))
    # The message names who is being waited on: "no se puede" is useless on a
    # shop-floor panel.
    assert "canteado" in str(exc.value).lower()
    mock_session.commit.assert_not_called()


def test_finished_names_every_open_activity(mock_session):
    order = _order(
        OrderStatus.in_process,
        _activity(ActivityType.cutting),
        _activity(ActivityType.banding, ActivityStatus.pending),
        _activity(ActivityType.additional, ActivityStatus.in_progress),
    )
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError) as exc:
        svc.transition(1, OrderStatus.finished, actor=_actor(UserRole.ADMIN))
    message = str(exc.value).lower()
    assert "canteado" in message and "adicionales" in message


# --- Finishing by hand (the derived path is covered in the activity tests) -------
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.BANDER])
def test_shop_floor_can_finish_when_the_work_is_done(mock_session, role):
    order = _order(OrderStatus.in_process)
    svc = _service(mock_session, order)
    svc.transition(1, OrderStatus.finished, actor=_actor(role))
    assert order.status == OrderStatus.finished.value
    mock_session.commit.assert_called_once()


# --- Dispatch (finished -> dispatched): admin/seller only ------------------------
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.BANDER])
def test_shop_floor_cannot_dispatch(mock_session, role):
    # Dispatch is a commercial act: the shop floor (operator/bander) can't register it.
    order = _order(OrderStatus.finished)
    svc = _service(mock_session, order)
    with pytest.raises(AuthorizationError):
        svc.transition(1, OrderStatus.dispatched, actor=_actor(role))
    mock_session.commit.assert_not_called()


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SELLER])
def test_admin_and_seller_can_dispatch(mock_session, role):
    order = _order(OrderStatus.finished)
    svc = _service(mock_session, order)
    svc.transition(1, OrderStatus.dispatched, actor=_actor(role))
    assert order.status == OrderStatus.dispatched.value
    assert order.dispatched_by_label == "Tester"
    mock_session.commit.assert_called_once()


# --- The admin rollback ----------------------------------------------------------
def test_rollback_reopens_the_cut_and_clears_the_assignment(mock_session):
    """It undoes somebody taking the wrong order, so the cut never started.

    The other activities are left alone: their clocks are sealed-once and wiping
    progress the bander declared would be destructive.
    """
    cut = _activity(ActivityType.cutting, ActivityStatus.in_progress)
    cut.started_at = datetime(2026, 1, 1)
    cut.started_by_label = "Operador"
    banding = _activity(ActivityType.banding, ActivityStatus.in_progress)
    banding.ready_at = datetime(2026, 1, 1)
    order = _order(OrderStatus.in_process, cut, banding)
    order.assigned_to_label = "Operador"
    order.queued_at = datetime(2026, 1, 1)
    svc = _service(mock_session, order)

    svc.transition(1, OrderStatus.queued, actor=_actor(UserRole.ADMIN))
    assert order.status == OrderStatus.queued.value
    assert order.assigned_to_label is None
    assert cut.status == ActivityStatus.pending.value
    assert cut.started_at is None and cut.started_by_label is None
    # Not re-dated, and the bander's clock is untouched.
    assert order.queued_at == datetime(2026, 1, 1)
    assert banding.status == ActivityStatus.in_progress.value
    assert banding.ready_at == datetime(2026, 1, 1)


# --- Pure helpers -----------------------------------------------------------------
def test_has_payment_true_only_when_some_amount_positive():
    assert _has_payment(None) is False
    assert (
        _has_payment(
            OrderPaymentInput(cash_amount=0, transfer_amount=0, credit_amount=0)
        )
        is False
    )
    assert _has_payment(OrderPaymentInput(cash_amount=10)) is True
    assert _has_payment(OrderPaymentInput(transfer_amount=7)) is True
    assert _has_payment(OrderPaymentInput(credit_amount=5)) is True


def test_progress_counts_cut_pieces():
    pieces = [
        SimpleNamespace(cut_at=datetime.utcnow()),
        SimpleNamespace(cut_at=None),
        SimpleNamespace(cut_at=None),
    ]
    progress = _progress(pieces)
    assert progress.cut_pieces == 1
    assert progress.total_pieces == 3


# --- The status clock ----------------------------------------------------------
def test_transition_seals_the_status_clock(mock_session):
    order = _order(OrderStatus.confirmed)
    order.status_changed_at = datetime(2026, 1, 1)
    svc = _service(mock_session, order)
    svc.transition(
        1,
        OrderStatus.queued,
        actor=_actor(UserRole.ADMIN),
        payment=OrderPaymentInput(cash_amount=10.0),
    )
    assert order.status_changed_at > datetime(2026, 1, 1)


def test_priority_does_not_restart_the_status_clock(mock_session):
    """Marking an order urgent is not a status change, and must not look like one.

    ``set_priority`` records itself as a history row with ``from == to``; if that
    also moved the clock, flagging an order would reset it to "just moved" and
    hide the very order somebody flagged because it was stuck.
    """
    order = _order(OrderStatus.queued)
    stamped = datetime(2026, 1, 1)
    order.status_changed_at = stamped
    svc = _service(mock_session, order)
    svc.set_priority(1, True, actor=_actor(UserRole.ADMIN))
    assert order.is_priority is True
    assert order.status_changed_at == stamped
