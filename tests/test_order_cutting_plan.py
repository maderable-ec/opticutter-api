"""Tests for the per-order cutting plan: materialization, touch marking, and the gate."""

from src.modules.branches.model import BranchModel
from src.modules.orders.model import OrderBoardModel, OrderPlacedPieceModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService


def _create_client(client, identifier="0100000397", phone="0100000397"):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": identifier,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": phone,
        },
    ).json()["data"]


def _create_board(client, code="MEL18"):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": f"Melamina {code}",
            "price": 45.5,
            "attributes": {"height": 2440, "width": 1220, "thickness": 18},
        },
    ).json()["data"]


# Default not halvable (both sides > half-width of 610) so the plan uses a full
# board; half-board tests live in test_optimizations/test_orders.
def _order_payload(client_id, product_id, height=800, width=700, quantity=3):
    return {
        "clientId": client_id,
        "branchId": 1,  # default branch seeded by conftest
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": height,
                "width": width,
                "quantity": quantity,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }


def _mint(client, db_session, payload):
    """Mints an order through the service (HTTP creation was removed) and reads it back via GET."""
    order = OrderService(db_session).create(OrderCreate.model_validate(payload))
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def _create_order(client, db_session, quantity=3, width=700):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"], quantity=quantity, width=width)
    return _mint(client, db_session, payload)


def _to_cutting(client, order_id):
    """Advances a freshly created order (confirmed) up to cutting."""
    for status in ("queued", "cutting"):
        body = {"status": status}
        if status == "queued":
            # Moving to the queue requires recording the (informational) payment method.
            body["payment"] = {"cashAmount": 100.0}
        resp = client.patch(f"/api/v1/orders/{order_id}/status", json=body)
        assert resp.status_code == 200
    return resp.json()["data"]


def _get_plan(client, order_id):
    resp = client.get(f"/api/v1/orders/{order_id}/cutting-plan")
    assert resp.status_code == 200
    return resp.json()["data"]


def test_create_order_materializes_cutting_plan(client, db_session):
    """Creating the order expands the snapshot into physical boards and placed pieces."""
    order = _create_order(client, db_session, quantity=3)
    plan = _get_plan(client, order["id"])

    assert plan["orderId"] == order["id"]
    assert plan["orderCode"] == order["code"]
    assert plan["status"] == "confirmed"
    assert plan["progress"] == {"cutPieces": 0, "totalPieces": 3}

    # 3 pieces of 800x700 fit in a single 2440x1220 board.
    assert len(plan["boards"]) == 1
    board = plan["boards"][0]
    assert board["sheetNumber"] == 1
    assert board["materialKey"] == "b1"
    assert board["productCode"] == "MEL18"
    assert board["width"] == 1220 and board["height"] == 2440
    assert board["progress"] == {"cutPieces": 0, "totalPieces": 3}

    # quantity=3 → 3 individual instances with their own identity (label#N).
    assert len(board["pieces"]) == 3
    assert {p["pieceId"] for p in board["pieces"]} == {
        "Puerta#1",
        "Puerta#2",
        "Puerta#3",
    }
    for piece in board["pieces"]:
        assert piece["label"] == "Puerta"
        assert piece["cut"] is False and piece["cutAt"] is None
        # Geometry ready to draw + nominal dims for grouping.
        assert {piece["originalWidth"], piece["originalHeight"]} == {700, 800}

    # Remainders and guillotine cuts from the snapshot, to draw the full
    # board in the workshop (free zones + saw lines).
    assert board["remainders"], "3 piezas chicas en 2440×1220 dejan sobrantes"
    for rem in board["remainders"]:
        assert set(rem) == {"x", "y", "width", "height"}
    assert board["cuts"], "colocar piezas genera recorridos de sierra"
    for cut in board["cuts"]:
        assert set(cut) == {"x", "y", "length", "isHorizontal"}


def test_cutting_plan_carries_the_commercial_reference(client, db_session):
    """The touch view shows the reference alongside the code, so the operator
    knows which job of the client is on the saw."""
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Obra Los Álamos — closets"
    order = _mint(client, db_session, payload)

    assert _get_plan(client, order["id"])["notes"] == "Obra Los Álamos — closets"


def test_cutting_plan_carries_the_branch_printing_switch(client, db_session):
    """The touch view gates its own label dispatch, so marking a piece cut in a shop
    with no thermal printer never fires the print request."""
    order = _create_order(client, db_session)
    assert _get_plan(client, order["id"])["printLabelsEnabled"] is True

    branch = db_session.get(BranchModel, 1)
    branch.print_labels_enabled = False
    db_session.commit()

    assert _get_plan(client, order["id"])["printLabelsEnabled"] is False


def test_cutting_plan_unknown_order_returns_404(client):
    resp = client.get("/api/v1/orders/99999/cutting-plan")
    assert resp.status_code == 404


def test_mark_piece_requires_cutting_state(client, db_session):
    """Pieces can only be marked while in 'cutting' state: before that it returns 422."""
    order = _create_order(client, db_session)
    piece_id = _get_plan(client, order["id"])["boards"][0]["pieces"][0]["id"]

    resp = client.patch(
        f"/api/v1/orders/{order['id']}/cutting-plan/pieces/{piece_id}",
        json={"cut": True},
    )
    assert resp.status_code == 422
    assert "corte" in resp.json()["errors"][0]["message"]


def test_mark_and_unmark_piece_updates_progress(client, db_session):
    order = _create_order(client, db_session, quantity=3)
    _to_cutting(client, order["id"])
    piece_id = _get_plan(client, order["id"])["boards"][0]["pieces"][0]["id"]
    url = f"/api/v1/orders/{order['id']}/cutting-plan/pieces/{piece_id}"

    marked = client.patch(url, json={"cut": True}).json()["data"]
    assert marked["piece"]["cut"] is True
    assert marked["piece"]["cutAt"] is not None
    assert marked["progress"] == {"cutPieces": 1, "totalPieces": 3}
    assert marked["boardProgress"] == {"cutPieces": 1, "totalPieces": 3}

    # Idempotent: re-marking doesn't change the cut timestamp.
    again = client.patch(url, json={"cut": True}).json()["data"]
    assert again["piece"]["cutAt"] == marked["piece"]["cutAt"]
    assert again["progress"] == {"cutPieces": 1, "totalPieces": 3}

    # Undo: the piece goes back to pending.
    undone = client.patch(url, json={"cut": False}).json()["data"]
    assert undone["piece"]["cut"] is False and undone["piece"]["cutAt"] is None
    assert undone["progress"] == {"cutPieces": 0, "totalPieces": 3}


def test_mark_piece_of_another_order_returns_404(client, db_session):
    """A piece belonging to another order doesn't exist for this one (404, no data leakage)."""
    c = _create_client(client)
    b = _create_board(client)
    first = _mint(client, db_session, _order_payload(c["id"], b["id"], width=600))
    second = _mint(client, db_session, _order_payload(c["id"], b["id"], width=500))
    _to_cutting(client, first["id"])
    foreign_piece = _get_plan(client, second["id"])["boards"][0]["pieces"][0]["id"]

    resp = client.patch(
        f"/api/v1/orders/{first['id']}/cutting-plan/pieces/{foreign_piece}",
        json={"cut": True},
    )
    assert resp.status_code == 404


def test_transition_to_cut_blocked_until_all_pieces_marked(client, db_session):
    """Cutting gate: cutting → cut requires the full cutting plan to be marked."""
    order = _create_order(client, db_session, quantity=3)
    _to_cutting(client, order["id"])
    pieces = _get_plan(client, order["id"])["boards"][0]["pieces"]

    # With 1 of 3 cut, the transition is rejected and reports what's pending.
    client.patch(
        f"/api/v1/orders/{order['id']}/cutting-plan/pieces/{pieces[0]['id']}",
        json={"cut": True},
    )
    blocked = client.patch(
        f"/api/v1/orders/{order['id']}/status", json={"status": "cut"}
    )
    assert blocked.status_code == 422
    assert "Faltan 2 pieza(s) por cortar" in blocked.json()["errors"][0]["message"]

    # With everything cut, the transition succeeds.
    for piece in pieces[1:]:
        client.patch(
            f"/api/v1/orders/{order['id']}/cutting-plan/pieces/{piece['id']}",
            json={"cut": True},
        )
    done = client.patch(f"/api/v1/orders/{order['id']}/status", json={"status": "cut"})
    assert done.status_code == 200
    assert done.json()["data"]["status"] == "cut"


def test_lazy_materialization_for_legacy_orders(client, db_session):
    """Orders predating the feature rebuild the plan from the snapshot."""
    order = _create_order(client, db_session, quantity=2)

    # Simulates an order created before the feature: no cutting plan rows.
    db_session.query(OrderPlacedPieceModel).delete()
    db_session.query(OrderBoardModel).delete()
    db_session.commit()

    plan = _get_plan(client, order["id"])
    assert plan["progress"]["totalPieces"] == 2
    assert len(plan["boards"]) == 1
    assert len(plan["boards"][0]["pieces"]) == 2
    # The snapshot keeps remainders and cuts: the rebuild brings them back.
    assert plan["boards"][0]["remainders"]
    assert plan["boards"][0]["cuts"]
