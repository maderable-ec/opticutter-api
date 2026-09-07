"""Unit: edge-banding track of ``OrderService.transition_banding`` (no DB).

A track parallel to cutting -- it advances ``pending -> in_progress -> done``
while the saw is still running -- but floored by it: starting needs the first
banded piece cut, finishing needs the last one. Same pattern as the cutting
state machine: transient ``OrderModel`` + ``get_scoped_or_404`` replaced; paths
that reject must not call ``commit``. The piece counts are stubbed here (they
are one aggregate query); ``tests/test_order_banding.py`` runs them for real.
"""

import pytest

from src.modules.orders.model import BandingStatus, OrderModel, OrderStatus
from src.modules.orders.schemas import CuttingProgress
from src.modules.orders.service import OrderService
from src.modules.users.enums import UserRole
from src.shared.audit import Actor, system_actor
from src.shared.exceptions import AuthorizationError, BusinessRuleError


def _order(
    banding_status: BandingStatus, *, status: OrderStatus = OrderStatus.cutting
) -> OrderModel:
    order = OrderModel(status=status.value, banding_status=banding_status.value)
    order.id = 1
    order.code = "ORD-2026-0001"
    return order


def _service(mock_session, order: OrderModel, *, banded=(1, 1)) -> OrderService:
    """``banded`` = (cut, total) banded pieces. The default clears both gates."""
    svc = OrderService(mock_session)
    svc.get_scoped_or_404 = lambda *a, **k: order
    svc._ensure_cutting_plan = lambda *a, **k: None
    svc._banded_progress = lambda *a, **k: CuttingProgress(
        cut_pieces=banded[0], total_pieces=banded[1]
    )
    return svc


def test_advance_pending_to_in_progress_commits(mock_session):
    order = _order(BandingStatus.pending)
    svc = _service(mock_session, order)
    resp = svc.transition_banding(1, BandingStatus.in_progress, actor=system_actor())
    assert order.banding_status == BandingStatus.in_progress.value
    assert resp.banding_status == BandingStatus.in_progress
    mock_session.commit.assert_called_once()


def test_reapplying_same_status_is_noop(mock_session):
    order = _order(BandingStatus.pending)
    svc = _service(mock_session, order)
    resp = svc.transition_banding(1, BandingStatus.pending, actor=system_actor())
    assert resp.banding_status == BandingStatus.pending
    mock_session.commit.assert_not_called()


def test_skipping_in_progress_is_invalid(mock_session):
    order = _order(BandingStatus.pending)
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_banding(1, BandingStatus.done, actor=system_actor())
    mock_session.commit.assert_not_called()


def test_order_without_banding_rejects(mock_session):
    order = _order(BandingStatus.not_applicable)
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_banding(1, BandingStatus.in_progress, actor=system_actor())
    mock_session.commit.assert_not_called()


def test_banding_requires_order_in_cutting_or_cut(mock_session):
    order = _order(BandingStatus.pending, status=OrderStatus.confirmed)
    svc = _service(mock_session, order)
    with pytest.raises(BusinessRuleError):
        svc.transition_banding(1, BandingStatus.in_progress, actor=system_actor())
    mock_session.commit.assert_not_called()


def test_unauthorized_role_cannot_band(mock_session):
    order = _order(BandingStatus.pending)
    svc = _service(mock_session, order)
    seller = Actor("staff", user_id=2, label="Vendedor", role=UserRole.SELLER.value)
    with pytest.raises(AuthorizationError):
        svc.transition_banding(1, BandingStatus.in_progress, actor=seller)
    mock_session.commit.assert_not_called()


def test_start_blocked_without_a_cut_banded_piece(mock_session):
    """Taking the order releases nothing: there has to be a banded piece cut."""
    order = _order(BandingStatus.pending)
    svc = _service(mock_session, order, banded=(0, 3))
    with pytest.raises(BusinessRuleError):
        svc.transition_banding(1, BandingStatus.in_progress, actor=system_actor())
    assert order.banding_status == BandingStatus.pending.value
    mock_session.commit.assert_not_called()


def test_finish_blocked_while_banded_pieces_remain(mock_session):
    order = _order(BandingStatus.in_progress)
    svc = _service(mock_session, order, banded=(2, 3))
    with pytest.raises(BusinessRuleError):
        svc.transition_banding(1, BandingStatus.done, actor=system_actor())
    mock_session.commit.assert_not_called()


def test_finish_allowed_when_every_banded_piece_is_cut(mock_session):
    """Plain pieces never reach this count, so they can still be pending."""
    order = _order(BandingStatus.in_progress)
    svc = _service(mock_session, order, banded=(3, 3))
    resp = svc.transition_banding(1, BandingStatus.done, actor=system_actor())
    assert resp.banding_status == BandingStatus.done
    mock_session.commit.assert_called_once()
