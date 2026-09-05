"""Tests for the shared shop-floor board endpoint ``GET /orders/workshop-queue``.

The board is a self-sufficient card list (client + board/banding usage + progress)
for the operator AND the canteador (who lacks ``orders:read``). It lists orders from
the queue up to ``cut``; ``confirmed``/``completed``/etc. are excluded. Reuses the
catalog/order/token helpers of the banding-track suite.
"""

from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.orders.model import OrderModel
from tests.test_order_banding import (
    _BRANCH,
    _create_board,
    _create_client,
    _cut_all_pieces,
    _mint_order,
    _order_with_banding,
    _order_without_banding,
    _patch_banding,
    _patch_status,
    _to_cutting,
    _token_for,
)

_URL = "/api/v1/orders/workshop-queue"


def _billed_linear_m(db_session, order_id: int) -> float:
    """Reads the first edge-banding summary entry's billed linear meters straight
    from the order's frozen snapshot, so tests don't hardcode a waste-dependent
    number."""
    order = db_session.get(OrderModel, order_id)
    summary = order.optimization_snapshot["edge_bandings_summary"]
    return summary[0]["billed_linear_m"]


def test_workshop_queue_lists_queued_through_cut(client, db_session):
    """Orders in queued / cutting / cut all appear, with a self-sufficient projection."""
    queued = _order_with_banding(client, db_session, identifier="0100000181")
    assert _patch_status(client, queued["id"], "queued").status_code == 200

    cutting = _order_with_banding(client, db_session, identifier="0100000199")
    _to_cutting(client, cutting["id"])

    cut = _order_with_banding(client, db_session, identifier="0100000207")
    _to_cutting(client, cut["id"])
    _cut_all_pieces(client, cut["id"])
    assert _patch_status(client, cut["id"], "cut").status_code == 200

    board = client.get(_URL).json()["data"]
    by_id = {i["orderId"]: i for i in board}
    assert {queued["id"], cutting["id"], cut["id"]} <= set(by_id)

    item = by_id[cut["id"]]
    assert item["status"] == "cut"
    assert item["bandingStatus"] == "pending"
    assert item["client"]["firstName"] == "Ada"
    # 1 board, one piece 500×1000mm fits on a single (half) MEL0207 sheet. The
    # row is named after the WHOLE board -- the "(medio tablero)" suffix is a
    # billing label, and the shop fetches the product, not the line.
    assert item["boardUsage"] == [
        {
            "materialKey": "b1",
            "name": "Melamina MEL0207",
            "count": 1,
            "fullCount": 0,
            "halfCount": 1,
        }
    ]
    # suffix "0207" → product "Tapacanto TAP0207", bandType Suave. The type
    # travels canonical: the board paints it as a badge.
    assert item["bandingUsage"] == [
        {
            "name": "Tapacanto TAP0207",
            "bandType": "Soft",
            "linearM": _billed_linear_m(db_session, cut["id"]),
        }
    ]
    # Every piece was cut → progress is complete.
    assert item["progress"]["totalPieces"] >= 1
    assert item["progress"]["cutPieces"] == item["progress"]["totalPieces"]

    # A still-queued order shows zero cut progress.
    assert by_id[queued["id"]]["progress"]["cutPieces"] == 0


def test_workshop_queue_merges_the_half_board_into_its_material(client, db_session):
    """A material billed as a whole board PLUS a half is ONE row, not two.

    ``materials_summary`` is keyed by ``(material_key, half_board)``, so it bills
    the two separately and names the half with a " (medio tablero)" suffix. The
    shop fetches one product from the rack, so the board merges them and reports
    the split -- otherwise "1 tablero y medio" read as two materials.

    Six 600x1200 pieces on a 1220x2440 sheet: four fit a whole board, the tail of
    two fits a half (610 wide), and 45.50 + 25.03 beats a second whole board.
    The cutting parameters are pinned first -- the packing is the premise of the
    test, and the shop's real 10mm trim (which ``.env`` carries) leaves 1200mm of
    usable width, one millimetre short of two 600s plus the kerf.
    """
    assert (
        client.patch(
            "/api/v1/settings/cutting",
            json={
                "kerf": 5,
                "topTrim": 0,
                "bottomTrim": 0,
                "leftTrim": 0,
                "rightTrim": 0,
                "halfBoardMarkupPct": 0.10,
            },
        ).status_code
        == 200
    )
    c = _create_client(client, identifier="0100000280")
    b = _create_board(client, code="MEL0280")
    order = _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [
                {
                    "priority": 0,
                    "height": 1200,
                    "width": 600,
                    "quantity": 6,
                    "materialKey": "b1",
                    "label": "Costado",
                    "canRotate": False,
                }
            ],
        },
    )
    # Precondition: the plan really is a whole board plus a half -- two billing
    # lines sharing one material key. Without it the merge would prove nothing.
    summary = db_session.get(OrderModel, order["id"]).optimization_snapshot[
        "materials_summary"
    ]
    assert sorted(line["half_board"] for line in summary) == [False, True]

    assert _patch_status(client, order["id"], "queued").status_code == 200
    item = next(
        i for i in client.get(_URL).json()["data"] if i["orderId"] == order["id"]
    )
    assert item["boardUsage"] == [
        {
            "materialKey": "b1",
            "name": "Melamina MEL0280",
            "count": 2,
            "fullCount": 1,
            "halfCount": 1,
        }
    ]


def test_workshop_queue_carries_the_commercial_reference(client, db_session):
    """The card shows the order's ``notes`` (project/site): it's what tells apart
    several orders of the same client on the board."""
    order = _order_without_banding(client, db_session)
    db_session.get(OrderModel, order["id"]).notes = "Proyecto Casa Pérez — cocina"
    db_session.commit()
    assert _patch_status(client, order["id"], "queued").status_code == 200

    board = client.get(_URL).json()["data"]
    item = next(i for i in board if i["orderId"] == order["id"])
    assert item["notes"] == "Proyecto Casa Pérez — cocina"


def test_workshop_queue_carries_the_branch_printing_switch(client, db_session):
    """The card gates its own consolidated dispatch: the admin's board spans every
    branch, so the switch travels per item rather than once per response."""
    order = _order_without_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200

    item = next(
        i for i in client.get(_URL).json()["data"] if i["orderId"] == order["id"]
    )
    assert item["printConsolidatedEnabled"] is True  # default: the shop prints

    branch = db_session.get(
        BranchModel, db_session.get(OrderModel, order["id"]).branch_id
    )
    branch.print_consolidated_enabled = False
    db_session.commit()

    item = next(
        i for i in client.get(_URL).json()["data"] if i["orderId"] == order["id"]
    )
    assert item["printConsolidatedEnabled"] is False


def test_workshop_queue_excludes_confirmed_and_completed(client, db_session):
    """The board only spans the active shop-floor window (queued..cut)."""
    confirmed = _order_with_banding(client, db_session, identifier="0100000215")

    completed = _order_with_banding(client, db_session, identifier="0100000223")
    _to_cutting(client, completed["id"])
    assert _patch_banding(client, completed["id"], "in_progress").status_code == 200
    assert _patch_banding(client, completed["id"], "done").status_code == 200
    _cut_all_pieces(client, completed["id"])
    assert _patch_status(client, completed["id"], "cut").status_code == 200
    assert _patch_status(client, completed["id"], "completed").status_code == 200

    ids = {i["orderId"] for i in client.get(_URL).json()["data"]}
    assert confirmed["id"] not in ids  # not yet in the shop
    assert completed["id"] not in ids  # already closed


def test_workshop_queue_lists_banding_usage(client, db_session):
    """The card lists billed linear meters per tapacanto type (+ Suave/Duro) so the
    canteador knows how much material to prepare; an order without banding yields
    an empty list."""
    banded = _order_with_banding(client, db_session, identifier="0100000272")
    assert _patch_status(client, banded["id"], "queued").status_code == 200

    plain = _order_without_banding(client, db_session)
    assert _patch_status(client, plain["id"], "queued").status_code == 200

    by_id = {i["orderId"]: i for i in client.get(_URL).json()["data"]}
    # suffix "0272" → product "Tapacanto TAP0272", bandType Suave.
    assert by_id[banded["id"]]["bandingUsage"] == [
        {
            "name": "Tapacanto TAP0272",
            "bandType": "Soft",
            "linearM": _billed_linear_m(db_session, banded["id"]),
        }
    ]
    assert by_id[plain["id"]]["bandingUsage"] == []


def test_workshop_queue_is_fifo_oldest_first(client, db_session):
    """FIFO is still the contract between orders of equal priority."""
    o1 = _order_with_banding(client, db_session, identifier="0100000231")
    o2 = _order_with_banding(client, db_session, identifier="0100000249")
    _to_cutting(client, o1["id"])
    _to_cutting(client, o2["id"])
    board = client.get(_URL).json()["data"]
    seen = [i["orderId"] for i in board if i["orderId"] in {o1["id"], o2["id"]}]
    assert seen == [o1["id"], o2["id"]]
    # Nothing is prioritized by default -- that is what makes this plain FIFO.
    assert all(i["isPriority"] is False for i in board)


def test_workshop_queue_is_branch_scoped(client, db_session: Session):
    """The board is branch-isolated: workshop roles see only their own branch."""
    order = _order_with_banding(client, db_session, identifier="0100000264")
    _to_cutting(client, order["id"])

    db_session.add(BranchModel(code="SUCW", name="Sucursal Taller", is_active=True))
    db_session.commit()
    branch2 = db_session.query(BranchModel).filter(BranchModel.code == "SUCW").one()

    # A canteador from ANOTHER branch doesn't see this order.
    other = _token_for(
        client,
        db_session,
        "canteador",
        branch_id=branch2.id,
        email="canteadorw@empresa.com",
    )
    ids_other = {i["orderId"] for i in client.get(_URL, headers=other).json()["data"]}
    assert order["id"] not in ids_other

    # The operador from branch 1 (same as the order) does.
    same = _token_for(client, db_session, "operador")
    ids_same = {i["orderId"] for i in client.get(_URL, headers=same).json()["data"]}
    assert order["id"] in ids_same


def test_workshop_queue_rbac(client, db_session):
    """Operator + canteador reach the board; seller (no ``orders:workshop``) can't."""
    order = _order_with_banding(client, db_session, identifier="0100000256")
    _to_cutting(client, order["id"])

    # The canteador reaches it despite lacking ``orders:read`` (embedded client proves it).
    canteador = _token_for(client, db_session, "canteador")
    resp = client.get(_URL, headers=canteador)
    assert resp.status_code == 200
    item = next(i for i in resp.json()["data"] if i["orderId"] == order["id"])
    assert item["client"]["firstName"] == "Ada"

    assert (
        client.get(_URL, headers=_token_for(client, db_session, "operador")).status_code
        == 200
    )
    assert (
        client.get(_URL, headers=_token_for(client, db_session, "vendedor")).status_code
        == 403
    )
