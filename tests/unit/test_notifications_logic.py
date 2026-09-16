"""Unit: notification emission plan + read-marking ownership (no DB).

``resolve_plan`` is pure (maps a transition to who-gets-what) and exercised
directly. ``mark_read`` ownership is checked with ``mock_session``: a foreign
notification must raise 404 and never commit.
"""

import pytest

from src.modules.notifications.emitter import (
    _RENDERERS,
    _Audience,
    _branch_operators,
    _emit,
    _quote_owner_or_global,
    _render,
    notify_order_branch_changed,
    resolve_plan,
)
from src.modules.notifications.enums import NotificationType
from src.modules.notifications.service import NotificationService
from src.modules.orders.model import OrderStatus
from src.shared.audit import Actor
from src.shared.exceptions import EntityNotFoundError


# --- resolve_plan: which transitions notify, and whom ----------------------------
def _audiences(from_status, to_status):
    return [(p.type, p.audience) for p in resolve_plan(from_status, to_status)]


def test_finished_notifies_global_admins_sellers():
    """Normally derived from the last activity closing, so this is what tells
    the office the work is done without anybody pressing a button."""
    assert _audiences(OrderStatus.in_process, OrderStatus.finished) == [
        (NotificationType.order_completed, _Audience.GLOBAL_ADMINS_SELLERS)
    ]


def test_confirmed_to_queued_notifies_branch_operators():
    assert _audiences(OrderStatus.confirmed, OrderStatus.queued) == [
        (NotificationType.order_queued, _Audience.BRANCH_OPERATORS)
    ]


def test_rollback_to_queued_notifies_nobody():
    # Admin rollback ``in_process -> queued`` is not a real enqueue.
    assert resolve_plan(OrderStatus.in_process, OrderStatus.queued) == []


def test_every_cancellation_reaches_the_admins():
    """An order only exists because a client confirmed a quote, so killing one is
    a sale that died whether or not it ever reached the shop."""
    assert _audiences(OrderStatus.confirmed, OrderStatus.cancelled) == [
        (NotificationType.order_cancelled, _Audience.GLOBAL_ADMINS)
    ]


def test_cancelling_from_the_queue_also_reaches_the_branch_operators():
    """Two disjoint audiences on one transition -- which is why the map returns a
    list. The card vanishing off the board is the operators' half of it."""
    assert _audiences(OrderStatus.queued, OrderStatus.cancelled) == [
        (NotificationType.order_cancelled, _Audience.GLOBAL_ADMINS),
        (NotificationType.order_cancelled, _Audience.BRANCH_OPERATORS),
    ]


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (OrderStatus.queued, OrderStatus.in_process),
        (OrderStatus.finished, OrderStatus.dispatched),
    ],
)
def test_other_transitions_are_not_notified(from_status, to_status):
    assert resolve_plan(from_status, to_status) == []


# --- the cancellation copy turns on where it was cancelled from -------------------
def test_cancelled_copy_names_the_queue_only_when_it_left_one():
    order = _Order(id=1, code="ORD-2026-0042")
    from_queue = _render(
        NotificationType.order_cancelled,
        order,
        {"fromStatus": OrderStatus.queued.value},
    )
    from_confirmed = _render(
        NotificationType.order_cancelled,
        order,
        {"fromStatus": OrderStatus.confirmed.value},
    )
    assert "cola de producción" in from_queue[1]
    # The office is being told a sale died, not that a board lost a card.
    assert "cola" not in from_confirmed[1]
    assert from_queue[0] == from_confirmed[0]


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


# --- render map: exhaustive, and sized for its columns ----------------------------
def test_every_notification_type_has_a_renderer():
    """The old if/else shipped the "queued" copy for anything it didn't know,
    so a new type was wrong in silence. This is the test that closes that."""
    assert set(_RENDERERS) == set(NotificationType)


@pytest.mark.parametrize("notification_type", list(NotificationType))
def test_renderers_fit_the_column_limits(notification_type):
    # Worst case: the longest code and a branch name filling its own column.
    order = _Order(id=1, code="O" * 32)
    payload = {"preorderCode": "P" * 32, "fromBranch": "B" * 128, "toBranch": "B" * 128}
    title, body = _render(notification_type, order, payload)
    assert 0 < len(title) <= 128  # NotificationModel.title
    assert 0 < len(body) <= 255  # NotificationModel.body


def test_confirmed_render_names_both_codes():
    title, body = _render(
        NotificationType.order_confirmed,
        _Order(id=1, code="ORD-2026-0007"),
        {"preorderCode": "PRE-2026-0042"},
    )
    assert "ORD-2026-0007" in title
    assert "PRE-2026-0042" in body and "ORD-2026-0007" in body


def test_confirmed_render_without_the_quote_code_does_not_print_none():
    _, body = _render(NotificationType.order_confirmed, _Order(id=1, code="ORD-1"), {})
    assert "None" not in body


def test_branch_renders_name_the_side_the_recipient_cares_about():
    payload = {"fromBranch": "Casa Matriz", "toBranch": "Sucursal Sur"}
    order = _Order(id=1, code="ORD-1")
    _, arrived = _render(NotificationType.order_branch_arrived, order, payload)
    _, left = _render(NotificationType.order_branch_left, order, payload)
    assert "Casa Matriz" in arrived and "Sucursal Sur" not in arrived
    assert "Sucursal Sur" in left and "Casa Matriz" not in left


def test_render_falls_back_to_the_id_when_the_order_has_no_code():
    title, _ = _render(NotificationType.order_queued, _Order(id=42, code=None), {})
    assert "#42" in title


# --- quote owner resolution + fallback ------------------------------------------
def test_quote_owner_receives_it_when_active(mock_session):
    owner = _User(id=3, is_active=True)
    mock_session.get.return_value = owner
    assert _quote_owner_or_global(mock_session, 3) == [owner]
    # The office was never queried: the owner is enough.
    mock_session.query.assert_not_called()


@pytest.mark.parametrize(
    "user_id,owner",
    [
        (3, "deleted"),
        (3, "deactivated"),
        (None, "never stamped"),  # the quote carries no owner at all
    ],
)
def test_quote_owner_falls_back_to_the_office(mock_session, user_id, owner):
    """A closed sale must not be lost to a seller who is no longer around."""
    office = [_User(id=1, is_active=True), _User(id=2, is_active=True)]
    mock_session.get.return_value = (
        _User(id=3, is_active=False) if owner == "deactivated" else None
    )
    mock_session.query.return_value.filter.return_value.all.return_value = office
    assert _quote_owner_or_global(mock_session, user_id) == office


def test_branch_operators_of_no_branch_is_nobody(mock_session):
    """The global roles carry ``branch_id = None``; there is no shop floor there."""
    assert _branch_operators(mock_session, None) == []
    mock_session.query.assert_not_called()


# --- _emit: the module's only try/except -----------------------------------------
def test_emit_excludes_the_actor(mock_session):
    recipients = [_User(id=1), _User(id=2), _User(id=3)]
    _emit(
        mock_session,
        _Order(id=7, code="ORD-7"),
        NotificationType.order_queued,
        lambda db: recipients,
        actor=Actor("staff", user_id=2),
    )
    rows = mock_session.add_all.call_args[0][0]
    assert sorted(r.user_id for r in rows) == [1, 3]


def test_emit_without_recipients_writes_nothing(mock_session):
    _emit(
        mock_session,
        _Order(id=7, code="ORD-7"),
        NotificationType.order_queued,
        lambda db: [],
    )
    mock_session.add_all.assert_not_called()
    mock_session.commit.assert_not_called()


def test_emit_swallows_a_failing_resolver(mock_session):
    """Every emission runs after a commit, so a DB failure here must die here."""

    def _boom(db):
        raise RuntimeError("database is gone")

    _emit(
        mock_session,
        _Order(id=7, code="ORD-7"),
        NotificationType.order_queued,
        _boom,
    )
    mock_session.commit.assert_not_called()


# --- branch change: both directions, and only from the queue ---------------------
def test_branch_change_notifies_both_directions(mock_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.modules.notifications.emitter._emit",
        lambda db, order, t, resolver, actor=None, data=None: calls.append((t, data)),
    )
    notify_order_branch_changed(
        mock_session,
        _Order(id=7, code="ORD-7"),
        order_status=OrderStatus.queued,
        from_branch_id=1,
        to_branch_id=2,
        from_branch_name="Casa Matriz",
        to_branch_name="Sucursal Sur",
    )
    assert [t for t, _ in calls] == [
        NotificationType.order_branch_arrived,
        NotificationType.order_branch_left,
    ]
    assert calls[0][1]["fromBranch"] == "Casa Matriz"
    assert calls[0][1]["toBranch"] == "Sucursal Sur"


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.confirmed,  # never on the origin's board, not takeable yet
        OrderStatus.in_process,
        OrderStatus.dispatched,
    ],
)
def test_branch_change_is_announced_only_from_the_queue(mock_session, status):
    notify_order_branch_changed(
        mock_session,
        _Order(id=7, code="ORD-7"),
        order_status=status,
        from_branch_id=1,
        to_branch_id=2,
    )
    mock_session.add_all.assert_not_called()


class _Order:
    """Duck-typed stand-in for ``OrderModel`` (the emitter only reads these)."""

    def __init__(self, id, code, branch_id=1):
        self.id = id
        self.code = code
        self.branch_id = branch_id


class _User:
    """Stand-in for ``UserModel``."""

    def __init__(self, id, is_active=True):
        self.id = id
        self.is_active = is_active
