"""Unit: the three activities of ``OrderService.transition_activity`` (no DB).

Replaces ``test_banding_state_machine.py``: the banding is one activity of
three, and the interesting part is now that ONE table-driven rule covers all of
them -- each activity's floors are measured against its own piece set, which is
what keeps the tracks parallel (plain pieces never hold the banding back) while
still stopping a bander from declaring work on pieces nobody has cut.

Same pattern as the order's state machine: transient ``OrderModel`` +
``get_scoped_or_404`` replaced; paths that reject must not call ``commit``. The
piece counts are stubbed here (they are one aggregate query each);
``tests/test_order_activities.py`` runs them for real.
"""

import pytest

from src.modules.orders.model import (
    ActivityStatus,
    ActivityType,
    OrderActivityModel,
    OrderModel,
    OrderStatus,
)
from src.modules.orders.schemas import CuttingProgress
from src.modules.orders.service import OrderService
from src.modules.users.enums import UserRole
from src.shared.audit import Actor
from src.shared.exceptions import AuthorizationError, BusinessRuleError

_BANDER = Actor("staff", user_id=3, label="Canteador", roles=(UserRole.BANDER.value,))
_OPERATOR = Actor(
    "staff", user_id=2, label="Operador", roles=(UserRole.OPERATOR.value,)
)
# A bander learning to cut: both workshop roles on one user.
_APPRENTICE = Actor(
    "staff",
    user_id=5,
    label="Aprendiz",
    roles=(UserRole.OPERATOR.value, UserRole.BANDER.value),
)


def _order(
    *statuses: tuple[ActivityType, ActivityStatus],
    status: OrderStatus = OrderStatus.in_process,
) -> OrderModel:
    order = OrderModel(status=status.value)
    order.id = 1
    order.code = "ORD-2026-0001"
    order.activities = [
        OrderActivityModel(type=t.value, status=s.value) for t, s in statuses
    ]
    return order


def _service(
    mock_session, order: OrderModel, *, banded=(1, 1), worked=(1, 1), pieces=(1, 1)
) -> OrderService:
    """``banded``/``worked``/``pieces`` = (cut, total). The defaults clear every floor."""
    svc = OrderService(mock_session)
    svc.get_scoped_or_404 = lambda *a, **k: order
    svc._ensure_cutting_plan = lambda *a, **k: None
    svc._set_progress = lambda *a, **k: {
        name: CuttingProgress(cut_pieces=cut, total_pieces=total)
        for name, (cut, total) in (
            ("all", pieces),
            ("banded", banded),
            ("worked", worked),
        )
    }
    return svc


def _cut(status: ActivityStatus) -> tuple[ActivityType, ActivityStatus]:
    return (ActivityType.cutting, status)


def _banding(status: ActivityStatus) -> tuple[ActivityType, ActivityStatus]:
    return (ActivityType.banding, status)


def _additional(status: ActivityStatus) -> tuple[ActivityType, ActivityStatus]:
    return (ActivityType.additional, status)


# --------------------------------------------------------------------------- #
# The moves themselves
# --------------------------------------------------------------------------- #
def test_advance_pending_to_in_progress_commits(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order)
    resp = svc.transition_activity(
        1, ActivityType.banding, ActivityStatus.in_progress, actor=_BANDER
    )
    assert resp.activity.status == ActivityStatus.in_progress
    assert resp.activity.started_by_label == "Canteador"
    mock_session.commit.assert_called_once()


def test_reapplying_same_status_is_noop(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order)
    resp = svc.transition_activity(
        1, ActivityType.banding, ActivityStatus.pending, actor=_BANDER
    )
    assert resp.activity.status == ActivityStatus.pending
    mock_session.commit.assert_not_called()


def test_skipping_in_progress_is_invalid(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.done, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


def test_missing_activity_rejects(mock_session):
    """No row = the activity does not apply to this order."""
    order = _order(_cut(ActivityStatus.in_progress))
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.in_progress, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


def test_activity_requires_the_order_in_process(mock_session):
    order = _order(
        _cut(ActivityStatus.pending),
        _banding(ActivityStatus.pending),
        status=OrderStatus.confirmed,
    )
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.in_progress, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


# --------------------------------------------------------------------------- #
# Role per activity (ACTIVITY_ROLES, not one flat tuple)
# --------------------------------------------------------------------------- #
def test_operator_cannot_band(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order)
    with pytest.raises(AuthorizationError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.in_progress, actor=_OPERATOR
        )
    mock_session.commit.assert_not_called()


def test_bander_cannot_cut(mock_session):
    order = _order(_cut(ActivityStatus.pending))
    svc = _service(mock_session, order)
    with pytest.raises(AuthorizationError):
        svc.transition_activity(
            1, ActivityType.cutting, ActivityStatus.in_progress, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


def test_seller_cannot_touch_the_shop_floor(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order)
    seller = Actor("staff", user_id=4, label="Vendedor", roles=(UserRole.SELLER.value,))
    with pytest.raises(AuthorizationError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.in_progress, actor=seller
        )
    mock_session.commit.assert_not_called()


# --------------------------------------------------------------------------- #
# The floors, per piece set
# --------------------------------------------------------------------------- #
def test_banding_start_blocked_without_a_cut_banded_piece(mock_session):
    """Taking the order releases nothing: there has to be a banded piece cut."""
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order, banded=(0, 3))
    with pytest.raises(BusinessRuleError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.in_progress, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


def test_banding_finish_blocked_while_banded_pieces_remain(mock_session):
    order = _order(
        _cut(ActivityStatus.in_progress), _banding(ActivityStatus.in_progress)
    )
    svc = _service(mock_session, order, banded=(2, 3))
    with pytest.raises(BusinessRuleError):
        svc.transition_activity(
            1, ActivityType.banding, ActivityStatus.done, actor=_BANDER
        )
    mock_session.commit.assert_not_called()


def test_banding_finish_allowed_with_plain_pieces_uncut(mock_session):
    """The parallel track: only the BANDED count reaches this floor."""
    order = _order(
        _cut(ActivityStatus.in_progress), _banding(ActivityStatus.in_progress)
    )
    svc = _service(mock_session, order, banded=(3, 3), pieces=(3, 9))
    resp = svc.transition_activity(
        1, ActivityType.banding, ActivityStatus.done, actor=_BANDER
    )
    assert resp.activity.status == ActivityStatus.done
    mock_session.commit.assert_called_once()


def test_additional_start_blocked_without_a_cut_worked_piece(mock_session):
    """Plain pieces on the saw release nothing: it waits for a piece with a code."""
    order = _order(
        _cut(ActivityStatus.in_progress), _additional(ActivityStatus.pending)
    )
    svc = _service(mock_session, order, pieces=(3, 9), worked=(0, 2))
    with pytest.raises(BusinessRuleError) as exc:
        svc.transition_activity(
            1, ActivityType.additional, ActivityStatus.in_progress, actor=_BANDER
        )
    assert "con trabajo de taller" in str(exc.value)
    mock_session.commit.assert_not_called()


def test_additional_finish_blocked_while_worked_pieces_remain(mock_session):
    """It used to close free; the codes say which pieces the work is on now."""
    order = _order(
        _cut(ActivityStatus.in_progress), _additional(ActivityStatus.in_progress)
    )
    svc = _service(mock_session, order, pieces=(9, 9), worked=(1, 2))
    with pytest.raises(BusinessRuleError) as exc:
        svc.transition_activity(
            1, ActivityType.additional, ActivityStatus.done, actor=_BANDER
        )
    assert "1 pieza(s) con trabajo de taller por cortar" in str(exc.value)
    mock_session.commit.assert_not_called()


def test_additional_finish_allowed_with_plain_pieces_uncut(mock_session):
    """The parallel track, as for the banding: only the WORKED count gates it,
    and the order still waits for the cut."""
    order = _order(
        _cut(ActivityStatus.in_progress), _additional(ActivityStatus.in_progress)
    )
    svc = _service(mock_session, order, pieces=(2, 9), worked=(2, 2))
    resp = svc.transition_activity(
        1, ActivityType.additional, ActivityStatus.done, actor=_BANDER
    )
    assert resp.activity.status == ActivityStatus.done
    assert resp.order_status == OrderStatus.in_process
    mock_session.commit.assert_called_once()


def test_cut_finish_needs_every_piece(mock_session):
    order = _order(_cut(ActivityStatus.in_progress))
    svc = _service(mock_session, order, pieces=(8, 9))
    with pytest.raises(BusinessRuleError) as exc:
        svc.transition_activity(
            1, ActivityType.cutting, ActivityStatus.done, actor=_OPERATOR
        )
    assert "1 pieza(s) por cortar" in str(exc.value)
    mock_session.commit.assert_not_called()


# --------------------------------------------------------------------------- #
# The transitions the activities DERIVE on the order
# --------------------------------------------------------------------------- #
def test_starting_the_cut_moves_the_order_out_of_the_queue(mock_session):
    order = _order(_cut(ActivityStatus.pending), status=OrderStatus.queued)
    svc = _service(mock_session, order, pieces=(0, 4))
    resp = svc.transition_activity(
        1, ActivityType.cutting, ActivityStatus.in_progress, actor=_OPERATOR
    )
    assert resp.order_status == OrderStatus.in_process
    assert order.status == OrderStatus.in_process.value
    assert order.assigned_to_label == "Operador"
    mock_session.commit.assert_called_once()


def test_closing_the_last_activity_finishes_the_order(mock_session):
    order = _order(_cut(ActivityStatus.in_progress))
    svc = _service(mock_session, order, pieces=(4, 4))
    resp = svc.transition_activity(
        1, ActivityType.cutting, ActivityStatus.done, actor=_OPERATOR
    )
    assert resp.order_status == OrderStatus.finished
    assert order.status == OrderStatus.finished.value


def test_the_order_waits_for_the_other_activities(mock_session):
    order = _order(_cut(ActivityStatus.in_progress), _banding(ActivityStatus.pending))
    svc = _service(mock_session, order, pieces=(4, 4), banded=(4, 4))
    resp = svc.transition_activity(
        1, ActivityType.cutting, ActivityStatus.done, actor=_OPERATOR
    )
    assert resp.order_status == OrderStatus.in_process
    assert order.status == OrderStatus.in_process.value


# --------------------------------------------------------------------------- #
# Several roles: the permissions are their union
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "activity_type",
    [ActivityType.cutting, ActivityType.banding, ActivityType.additional],
)
def test_the_apprentice_works_every_activity(mock_session, activity_type):
    """Operador + canteador registers the cut without giving up the banding."""
    order = _order(
        _cut(ActivityStatus.in_progress),
        _banding(ActivityStatus.pending),
        _additional(ActivityStatus.pending),
    )
    if activity_type is ActivityType.cutting:
        order.activities[0].status = ActivityStatus.pending.value
    svc = _service(mock_session, order)
    resp = svc.transition_activity(
        1, activity_type, ActivityStatus.in_progress, actor=_APPRENTICE
    )
    assert resp.activity.status == ActivityStatus.in_progress
    assert resp.activity.started_by_label == "Aprendiz"
    mock_session.commit.assert_called_once()


def test_the_apprentice_takes_the_order_from_the_queue(mock_session):
    """Starting the cut derives ``queued -> in_process``, an operator's move."""
    order = _order(_cut(ActivityStatus.pending), status=OrderStatus.queued)
    svc = _service(mock_session, order, pieces=(0, 4))
    resp = svc.transition_activity(
        1, ActivityType.cutting, ActivityStatus.in_progress, actor=_APPRENTICE
    )
    assert resp.order_status == OrderStatus.in_process
    assert order.assigned_to_label == "Aprendiz"
