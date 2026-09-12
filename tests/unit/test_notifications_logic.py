"""Unit: notification emission plan + read-marking ownership (no DB).

``resolve_plan`` is pure (maps a transition to who-gets-what) and exercised
directly. ``mark_read`` ownership is checked with ``mock_session``: a foreign
notification must raise 404 and never commit.
"""

import pytest

from src.modules.notifications.emitter import _Audience, resolve_plan
from src.modules.notifications.enums import NotificationType
from src.modules.notifications.service import NotificationService
from src.modules.orders.model import OrderStatus
from src.shared.exceptions import EntityNotFoundError


# --- resolve_plan: which transitions notify, and whom ----------------------------
def test_finished_notifies_global_admins_sellers():
    """Normally derived from the last activity closing, so this is what tells
    the office the work is done without anybody pressing a button."""
    plan = resolve_plan(OrderStatus.in_process, OrderStatus.finished)
    assert plan is not None
    assert plan.type is NotificationType.order_completed
    assert plan.audience is _Audience.GLOBAL_ADMINS_SELLERS


def test_confirmed_to_queued_notifies_branch_operators():
    plan = resolve_plan(OrderStatus.confirmed, OrderStatus.queued)
    assert plan is not None
    assert plan.type is NotificationType.order_queued
    assert plan.audience is _Audience.BRANCH_OPERATORS


def test_rollback_to_queued_notifies_nobody():
    # Admin rollback ``in_process -> queued`` is not a real enqueue.
    assert resolve_plan(OrderStatus.in_process, OrderStatus.queued) is None


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (OrderStatus.queued, OrderStatus.in_process),
        (OrderStatus.finished, OrderStatus.dispatched),
        (OrderStatus.confirmed, OrderStatus.cancelled),
    ],
)
def test_other_transitions_are_not_notified(from_status, to_status):
    assert resolve_plan(from_status, to_status) is None


# --- mark_read ownership ----------------------------------------------------------
def test_mark_read_foreign_notification_raises_and_does_not_commit(mock_session):
    foreign = _Notification(id=7, user_id=99)
    mock_session.get.return_value = foreign
    svc = NotificationService(mock_session)
    with pytest.raises(EntityNotFoundError):
        svc.mark_read(7, user_id=1)
    mock_session.commit.assert_not_called()


def test_mark_read_missing_notification_raises(mock_session):
    mock_session.get.return_value = None
    svc = NotificationService(mock_session)
    with pytest.raises(EntityNotFoundError):
        svc.mark_read(123, user_id=1)
    mock_session.commit.assert_not_called()


def test_mark_read_already_read_is_idempotent(mock_session):
    from datetime import datetime

    already = _Notification(id=5, user_id=1, read_at=datetime.utcnow())
    mock_session.get.return_value = already
    svc = NotificationService(mock_session)
    result = svc.mark_read(5, user_id=1)
    assert result is already
    # No new write when it was already read.
    mock_session.commit.assert_not_called()


class _Notification:
    """Lightweight stand-in for ``NotificationModel`` (no ORM/session needed)."""

    def __init__(self, id, user_id, read_at=None):
        self.id = id
        self.user_id = user_id
        self.read_at = read_at
