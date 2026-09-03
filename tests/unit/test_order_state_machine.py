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

from src.modules.orders.model import OrderModel, OrderStatus
from src.modules.orders.schemas import OrderPaymentInput
from src.modules.orders.service import OrderService, _has_payment, _progress
from src.modules.users.enums import UserRole
from src.shared.audit import Actor
from src.shared.exceptions import (
    AuthorizationError,
    BusinessRuleError,
    ValidationError,
)


def _order(
    status: OrderStatus, *, banding_status: str = "not_applicable"
) -> OrderModel:
    """Transient order (no session) in the requested status."""
    order = OrderModel(status=status.value, banding_status=banding_status)
    order.id = 1
    return order


def _service(mock_session, order: OrderModel) -> OrderService:
    svc = OrderService(mock_session)
    # The branch-scoped id lookup is replaced with the order in hand.
    svc.get_scoped_or_404 = lambda *a, **k: order
    return svc


def _actor(role: UserRole | None) -> Actor:
    return Actor("staff", user_id=1, label="Tester", role=role.value if role else None)


# --- Invalid transitions / authorization ---------------------------------------
def test_invalid_transition_raises_and_does_not_commit(mock_session):
    svc = _service(mock_session, _order(OrderStatus.confirmed))
    with pytest.raises(BusinessRuleError):
        svc.transition(1, OrderStatus.cutting, actor=_actor(UserRole.ADMIN))
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


# --- Production gate (cutting -> cut) --------------------------------------------
def test_cut_blocked_while_pieces_pending(mock_session):
    order = _order(OrderStatus.cutting)
    svc = _service(mock_session, order)
    svc._ensure_cutting_plan = lambda o: None  # the plan is already materialized
    mock_session.query.return_value.filter.return_value.count.return_value = 2
    with pytest.raises(BusinessRuleError):
        svc.transition(1, OrderStatus.cut, actor=_actor(UserRole.OPERATOR))
    mock_session.commit.assert_not_called()


# --- Closing gate (banding pending) ----------------------------------------------
def test_completed_blocked_when_banding_pending(mock_session):
    order = _order(OrderStatus.cut, banding_status="in_progress")
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition(1, OrderStatus.completed, actor=_actor(UserRole.ADMIN))
    mock_session.commit.assert_not_called()


# --- Completion by the shop floor (operator / bander) ----------------------------
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.BANDER])
def test_shop_floor_can_complete_when_banding_settled(mock_session, role):
    # No banding (not_applicable) or banding done → operator and bander may complete.
    order = _order(OrderStatus.cut, banding_status="done")
    svc = _service(mock_session, order)
    svc.transition(1, OrderStatus.completed, actor=_actor(role))
    assert order.status == OrderStatus.completed.value
    mock_session.commit.assert_called_once()


def test_operator_cannot_complete_while_bander_still_banding(mock_session):
    # Scenario 1: the operator finished cutting but the bander is still banding;
    # Gate B blocks the operator from closing the order early.
    order = _order(OrderStatus.cut, banding_status="in_progress")
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition(1, OrderStatus.completed, actor=_actor(UserRole.OPERATOR))
    mock_session.commit.assert_not_called()


# --- Dispatch (completed -> despachado): admin/seller only -----------------------
@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.BANDER])
def test_shop_floor_cannot_dispatch(mock_session, role):
    # Dispatch is a commercial act: the shop floor (operator/bander) can't register it.
    order = _order(OrderStatus.completed)
    svc = _service(mock_session, order)
    with pytest.raises(AuthorizationError):
        svc.transition(1, OrderStatus.dispatched, actor=_actor(role))
    mock_session.commit.assert_not_called()


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SELLER])
def test_admin_and_seller_can_dispatch(mock_session, role):
    order = _order(OrderStatus.completed)
    svc = _service(mock_session, order)
    svc.transition(1, OrderStatus.dispatched, actor=_actor(role))
    assert order.status == OrderStatus.dispatched.value
    assert order.dispatched_by_label == "Tester"
    mock_session.commit.assert_called_once()


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
