"""Tests for cross-cutting audit: user attribution (who did what).

Covers the generic ``created_by``/``updated_by`` stamping (via request context),
actor attribution in orders/preorders status history, and recording who cut each
piece. The ``client`` fixture authenticates as the admin seeded by ``conftest``
(``Conftest Admin``), which is the expected actor.
"""

from src.modules.clients.model import ClientModel
from src.modules.products.model import ProductModel
from src.modules.users.service import UserService

from .test_orders import (
    _create_board,
    _create_client,
    _mint_order,
    _order_payload,
)

_ADMIN_EMAIL = "conftest-admin@empresa.com"
_ADMIN_NAME = "Conftest Admin"


def _admin(db_session):
    return UserService(db_session).get_by_email(_ADMIN_EMAIL)


# ---------------------------------------------------------------------------
# Generic created_by / updated_by (CRUDService + request context)
# ---------------------------------------------------------------------------


def test_client_create_stamps_created_by(client, db_session):
    created = _create_client(client)
    admin = _admin(db_session)

    db_session.expire_all()
    row = db_session.get(ClientModel, created["id"])
    assert row.created_by == admin.id
    assert row.updated_by == admin.id
    assert row.created_at is not None


def test_product_create_stamps_created_by(client, db_session):
    created = _create_board(client)
    admin = _admin(db_session)

    db_session.expire_all()
    row = db_session.get(ProductModel, created["id"])
    assert row.created_by == admin.id
    assert row.updated_by == admin.id


def test_client_update_stamps_updated_by_only(client, db_session):
    created = _create_client(client)
    admin = _admin(db_session)
    client.put(
        f"/api/v1/clients/{created['id']}",
        json={"identifier": "0100000397", "firstName": "Grace"},
    )

    db_session.expire_all()
    row = db_session.get(ClientModel, created["id"])
    assert row.updated_by == admin.id


# ---------------------------------------------------------------------------
# Actor attribution in order transitions
# ---------------------------------------------------------------------------


def test_order_status_transition_records_staff_actor(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _mint_order(db_session, _order_payload(c["id"], b["id"]))
    admin = _admin(db_session)

    resp = client.patch(
        f"/api/v1/orders/{order.id}/status",
        json={"status": "queued", "payment": {"cashAmount": 100.0}},
    )
    assert resp.status_code == 200

    last = resp.json()["data"]["history"][-1]
    assert last["toStatus"] == "queued"
    assert last["actor"] == "staff"
    assert last["actorUserId"] == admin.id
    assert last["actorLabel"] == _ADMIN_NAME


def test_mark_piece_cut_records_cut_by(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _mint_order(db_session, _order_payload(c["id"], b["id"]))
    admin = _admin(db_session)
    for status in ("queued", "cutting"):
        body = {"status": status}
        if status == "queued":
            body["payment"] = {"cashAmount": 100.0}
        client.patch(f"/api/v1/orders/{order.id}/status", json=body)

    plan = client.get(f"/api/v1/orders/{order.id}/cutting-plan").json()["data"]
    piece_id = plan["boards"][0]["pieces"][0]["id"]

    resp = client.patch(
        f"/api/v1/orders/{order.id}/cutting-plan/pieces/{piece_id}", json={"cut": True}
    )
    assert resp.status_code == 200
    piece = resp.json()["data"]["piece"]
    assert piece["cut"] is True
    assert piece["cutBy"] == admin.id
    assert piece["cutByLabel"] == _ADMIN_NAME

    # Unmarking clears the attribution along with cut_at.
    undo = client.patch(
        f"/api/v1/orders/{order.id}/cutting-plan/pieces/{piece_id}", json={"cut": False}
    )
    undone = undo.json()["data"]["piece"]
    assert undone["cut"] is False
    assert undone["cutBy"] is None
    assert undone["cutByLabel"] is None


# ---------------------------------------------------------------------------
# Pre-order history + client attribution on confirm
# ---------------------------------------------------------------------------


def _create_preorder(client, c, b):
    return client.post(
        "/api/v1/preorders/", json=_order_payload(c["id"], b["id"])
    ).json()["data"]


def test_preorder_create_records_staff_draft(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    admin = _admin(db_session)

    pre = _create_preorder(client, c, b)
    history = pre["history"]
    assert len(history) == 1
    assert history[0]["fromStatus"] is None
    assert history[0]["toStatus"] == "draft"
    assert history[0]["actor"] == "staff"
    assert history[0]["actorUserId"] == admin.id
    assert history[0]["actorLabel"] == _ADMIN_NAME


def test_preorder_client_confirm_attributes_to_client(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    pre = _create_preorder(client, c, b)

    # Staff sends the link (transition to 'sent' by staff)...
    link = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    # ...and the client confirms from the public link.
    confirmed = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert confirmed.status_code == 200

    detail = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    by_status = {h["toStatus"]: h for h in detail["history"]}
    assert by_status["sent"]["actor"] == "staff"
    assert by_status["confirmed"]["actor"] == "client"
    assert by_status["confirmed"]["actorUserId"] is None

    # The order minted by the confirmation is born attributed to the client.
    order = client.get(f"/api/v1/orders/{detail['orderId']}").json()["data"]
    assert order["history"][0]["actor"] == "client"
    assert order["history"][0]["actorUserId"] is None
