"""Tests for reassigning an order's branch: ``PATCH /orders/{id}/branch``.

Load rebalancing when a branch is saturated: admin/seller move a
``confirmed``/``queued`` order to another active branch (before the shop floor
starts). The move reprints documents under the new branch, and a ``queued``
order re-notifies the new branch's operators. Reuses the catalog/order/token
helpers of the banding-track suite.
"""

from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.orders.model import OrderStatusHistoryModel
from tests.test_order_banding import (
    _BRANCH,
    _create_board,
    _create_client,
    _create_edge_banding,
    _mint_order,
    _order_with_banding,
    _patch_status,
    _to_cutting,
    _token_for,
)


def _make_branch(db_session, code="SUCX", name="Sucursal X", active=True):
    db_session.add(BranchModel(code=code, name=name, is_active=active))
    db_session.commit()
    return db_session.query(BranchModel).filter(BranchModel.code == code).one()


def _change_branch(client, oid, branch_id, **kw):
    return client.patch(
        f"/api/v1/orders/{oid}/branch", json={"branchId": branch_id}, **kw
    )


def test_admin_moves_confirmed_order_to_another_branch(client, db_session: Session):
    """A confirmed order moves to another active branch; documents reprint there."""
    order = _order_with_banding(client, db_session, identifier="0100000280")
    branch2 = _make_branch(db_session)

    resp = _change_branch(client, order["id"], branch2.id)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["branch"]["id"] == branch2.id
    assert data["branch"]["code"] == "SUCX"

    # The order document renders under the new branch (live letterhead lookup).
    assert client.get(f"/api/v1/orders/{order['id']}/document").status_code == 200

    # The move is audited in the status history.
    rows = (
        db_session.query(OrderStatusHistoryModel)
        .filter(OrderStatusHistoryModel.order_id == order["id"])
        .all()
    )
    assert any("Sucursal cambiada" in (r.note or "") for r in rows)


def test_seller_can_change_branch(client, db_session: Session):
    """The seller (global scope, ``orders:write``) can also rebalance a branch."""
    order = _order_with_banding(client, db_session, identifier="0100000298")
    branch2 = _make_branch(db_session)
    seller = _token_for(client, db_session, "vendedor")

    resp = _change_branch(client, order["id"], branch2.id, headers=seller)
    assert resp.status_code == 200
    assert resp.json()["data"]["branch"]["id"] == branch2.id


def test_moving_queued_order_notifies_new_branch_operators(client, db_session: Session):
    """A queued order landing in the new branch notifies that branch's operators."""
    order = _order_with_banding(client, db_session, identifier="0100000306")
    assert _patch_status(client, order["id"], "queued").status_code == 200
    branch2 = _make_branch(db_session)
    # Operator of the NEW branch, created before the move so they're a recipient.
    op2 = _token_for(
        client, db_session, "operador", branch_id=branch2.id, email="op2@empresa.com"
    )

    assert _change_branch(client, order["id"], branch2.id).status_code == 200

    count = client.get("/api/v1/notifications/unread-count", headers=op2).json()["data"]
    assert count["count"] >= 1


def test_cannot_change_branch_once_cutting_started(client, db_session: Session):
    """Frozen once the shop floor starts: 'cutting' rejects the move (422)."""
    order = _order_with_banding(client, db_session, identifier="0100000314")
    _to_cutting(client, order["id"])  # confirmed → queued → cutting
    branch2 = _make_branch(db_session)

    assert _change_branch(client, order["id"], branch2.id).status_code == 422


def test_change_branch_rejects_inactive_target(client, db_session: Session):
    order = _order_with_banding(client, db_session, identifier="0100000322")
    inactive = _make_branch(db_session, code="SUCI", name="Inactiva", active=False)

    assert _change_branch(client, order["id"], inactive.id).status_code == 422


def test_change_branch_rejects_unknown_target(client, db_session: Session):
    order = _order_with_banding(client, db_session, identifier="0100000330")

    assert _change_branch(client, order["id"], 999999).status_code == 404


def test_operator_cannot_change_branch(client, db_session: Session):
    """The operator lacks ``orders:write`` → 403 (can't move load between branches)."""
    order = _order_with_banding(client, db_session, identifier="0100000348")
    branch2 = _make_branch(db_session)
    op = _token_for(client, db_session, "operador")

    assert (
        _change_branch(client, order["id"], branch2.id, headers=op).status_code == 403
    )


def test_change_branch_conflicts_with_active_duplicate(client, db_session: Session):
    """Preserves the "one active identical order per branch" invariant (409)."""
    branch2 = _make_branch(db_session)
    c = _create_client(client, identifier="0100000355")
    b = _create_board(client, code="MEL0108")
    eb = _create_edge_banding(client, code="TAP0108")
    payload = {
        "clientId": c["id"],
        "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
        "requirements": [
            {
                "priority": 0,
                "height": 500,
                "width": 1000,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Costado",
                "canRotate": True,
                "edgeBanding": {"productId": eb["id"], "sides": ["top", "bottom"]},
            }
        ],
    }
    a = _mint_order(client, db_session, {**payload, "branchId": _BRANCH})
    # Same client + hash + tier already active in branch2.
    _mint_order(client, db_session, {**payload, "branchId": branch2.id})

    assert _change_branch(client, a["id"], branch2.id).status_code == 409
