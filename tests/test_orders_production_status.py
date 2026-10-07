"""``GET /orders/production-status``: what each branch's saw is doing right now.

The endpoint reads the real clock, so the events are seeded relative to it.
The rules themselves are pinned without a database in
``tests/unit/test_production.py``; this covers the reading, the branches and
who may ask.
"""

from datetime import datetime, timedelta

from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    OrderActivityModel,
    OrderBoardModel,
    OrderModel,
    OrderPlacedPieceModel,
)
from tests.order_helpers import _token_for

_URL = "/api/v1/orders/production-status"


def _seed(db):
    """Matriz cutting now, Macas stopped with work waiting, Norte idle."""
    now = datetime.utcnow()
    macas = BranchModel(code="MACAS", name="Macas", is_active=True)
    norte = BranchModel(code="NORTE", name="Norte", is_active=True)
    gone = BranchModel(code="OLD", name="Cerrada", is_active=False)
    client = ClientModel(identifier="0100000397")
    db.add_all([macas, norte, gone, client])
    db.commit()

    def order(branch_id, status, code):
        o = OrderModel(
            client_id=client.id,
            branch_id=branch_id,
            status=status,
            optimization_snapshot={},
            optimization_hash=code,
            currency="USD",
            subtotal=10.0,
            total=10.0,
            total_boards_used=1,
            code=code,
        )
        db.add(o)
        db.commit()
        return o

    def piece(o, at, n):
        board = OrderBoardModel(
            order_id=o.id,
            sheet_number=n,
            material_key="m",
            width=2440,
            height=1220,
            thickness=18,
        )
        db.add(board)
        db.commit()
        db.add(
            OrderPlacedPieceModel(
                order_id=o.id,
                board_id=board.id,
                piece_id=f"p#{n}",
                label="p",
                x=0,
                y=0,
                width=100,
                height=100,
                original_width=100,
                original_height=100,
                rotated=False,
                cut_at=at,
            )
        )
        db.commit()

    cutting = order(1, "in_process", "ORD-000001")
    db.add(
        OrderActivityModel(
            order_id=cutting.id,
            type="cutting",
            status="in_progress",
            started_at=now - timedelta(minutes=50),
        )
    )
    db.commit()
    piece(cutting, now - timedelta(minutes=40), 1)
    piece(cutting, now - timedelta(minutes=5), 2)
    piece(cutting, now - timedelta(minutes=2), 3)

    waiting = order(macas.id, "queued", "ORD-000002")
    earlier = order(macas.id, "finished", "ORD-000003")
    piece(earlier, now - timedelta(hours=2), 1)
    return now, macas, norte, waiting


def test_each_branch_reads_cutting_stopped_or_idle(client, db_session):
    now, macas, norte, _ = _seed(db_session)

    data = client.get(_URL).json()["data"]
    assert data["idleMinutes"] == 15
    matriz, other, quiet = data["branches"]  # active only, by id

    assert matriz["state"] == "cutting"
    assert matriz["cuttingOrderCodes"] == ["ORD-000001"]
    # The run began after the last stop: the 35-min gap before the 2nd piece.
    since = datetime.fromisoformat(matriz["since"].rstrip("Z"))
    assert abs(since - (now - timedelta(minutes=5))) < timedelta(seconds=2)

    assert other["branchId"] == macas.id
    assert other["state"] == "stopped"  # a queued order, nothing for 2 hours
    assert other["queuedCount"] == 1
    assert other["since"] == other["lastEventAt"]

    assert quiet["branchId"] == norte.id
    assert quiet["state"] == "idle"
    assert quiet["since"] is None and quiet["lastEventAt"] is None


def test_a_global_role_narrows_to_one_branch(client, db_session):
    _, macas, _, _ = _seed(db_session)
    data = client.get(_URL, params={"branchId": macas.id}).json()["data"]
    assert [b["branchId"] for b in data["branches"]] == [macas.id]


def test_the_seller_sees_it_and_the_operator_only_their_branch(client, db_session):
    _, macas, _, _ = _seed(db_session)

    seller = _token_for(client, db_session, "vendedor")
    resp = client.get(_URL, headers=seller)
    assert resp.status_code == 200
    assert len(resp.json()["data"]["branches"]) == 3

    operator = _token_for(client, db_session, "operador", branch_id=macas.id)
    resp = client.get(_URL, params={"branchId": 1}, headers=operator)
    assert [b["branchId"] for b in resp.json()["data"]["branches"]] == [macas.id]


def test_the_bander_cannot_read_it(client, db_session):
    _seed(db_session)
    bander = _token_for(client, db_session, "canteador")
    assert client.get(_URL, headers=bander).status_code == 403
