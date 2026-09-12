"""Tests for the 'despachado' (delivered to client) status and its paperwork.

Dispatch is the real close of the cycle: ``finished → dispatched`` (terminal). It is
a commercial act restricted to admin/seller by ``TRANSITION_ROLES`` — the shop floor
(operador/canteador) cannot register it. There is no dispatch sheet any more: the
delivery line, the liability disclaimer and the signature lines are part of the
order's one document.
"""

import io
from datetime import datetime

from pypdf import PdfReader

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from tests.order_helpers import _BRANCH, _patch_status, _to_finished, _token_for


def _create_client(client, identifier="0100000397"):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": identifier,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": identifier,
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


def _order_payload(client_id, product_id, width=600):
    return {
        "clientId": client_id,
        "branchId": _BRANCH,
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": width,
                "quantity": 2,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }


def _mint_order(client, db_session, identifier="0100000397", code="MEL18", width=600):
    """Mints through the service (HTTP creation was removed) and reads it back via GET."""
    c = _create_client(client, identifier=identifier)
    b = _create_board(client, code=code)
    order = OrderService(db_session).create(
        OrderCreate.model_validate(_order_payload(c["id"], b["id"], width=width))
    )
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


# --------------------------------------------------------------------------- #
# Transition to 'despachado'
# --------------------------------------------------------------------------- #
def test_dispatch_from_completed_freezes_metadata(client, db_session):
    order = _mint_order(client, db_session)
    _to_finished(client, order["id"])

    resp = _patch_status(client, order["id"], "dispatched")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "dispatched"
    # Dispatch freezes date and the responsible user (shown on the dispatch sheet).
    assert data["dispatchedAt"] is not None
    assert data["dispatchedByLabel"] == "Conftest Admin"
    # History: the last entry records finished → dispatched.
    assert data["history"][-1]["fromStatus"] == "finished"
    assert data["history"][-1]["toStatus"] == "dispatched"


def test_shop_floor_cannot_dispatch(client, db_session):
    """operador and canteador cannot register the dispatch (commercial act, admin/seller only)."""
    # A distinct (valid) cédula per role: the identifier is unique per client.
    cedulas = ("0100000900", "0100000918")
    for idx, role in enumerate(("operador", "canteador")):
        order = _mint_order(
            client,
            db_session,
            identifier=cedulas[idx],
            code=f"MELR{idx}",
            width=600 - idx * 50,
        )
        _to_finished(client, order["id"])
        headers = _token_for(client, db_session, role)
        resp = _patch_status(client, order["id"], "dispatched", headers=headers)
        assert resp.status_code == 403, role
        # The order stays finished: the rejected transition doesn't advance it.
        assert (
            client.get(f"/api/v1/orders/{order['id']}").json()["data"]["status"]
            == "finished"
        )


def test_seller_can_dispatch(client, db_session):
    """The seller (a commercial role) can register the dispatch."""
    order = _mint_order(
        client, db_session, identifier="0100000363", code="MELS0", width=550
    )
    _to_finished(client, order["id"])
    headers = _token_for(client, db_session, "vendedor")
    resp = _patch_status(client, order["id"], "dispatched", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "dispatched"


def test_dispatched_is_terminal(client, db_session):
    order = _mint_order(client, db_session)
    _to_finished(client, order["id"])
    assert _patch_status(client, order["id"], "dispatched").status_code == 200

    # No outgoing transitions: anything after this is invalid.
    bad = _patch_status(client, order["id"], "cancelled")
    assert bad.status_code == 422
    assert "inválida" in bad.json()["errors"][0]["message"]


def test_cannot_dispatch_before_finished(client, db_session):
    """Only finished orders can be dispatched: from 'confirmed' the transition is invalid."""
    order = _mint_order(client, db_session)
    bad = _patch_status(client, order["id"], "dispatched")
    assert bad.status_code == 422
    assert "inválida" in bad.json()["errors"][0]["message"]


# --------------------------------------------------------------------------- #
# The delivery block on the order's document
# --------------------------------------------------------------------------- #
def test_the_document_prints_the_frozen_dispatch_data(client, db_session):
    """The dispatch sheet is gone; its delivery line lives on the ORDEN DE PEDIDO.

    Before dispatch it prints rules to fill in by hand (the shop prints the
    document when the cutting is done, which is before anyone delivers); once
    dispatched it carries what the transition froze.
    """
    order = _mint_order(client, db_session)
    oid = order["id"]

    before = _document_text(client, oid)
    assert "Fecha de despacho" in before
    assert "____" in before

    _to_finished(client, oid)
    _patch_status(client, oid, "dispatched")

    after = _document_text(client, oid)
    assert datetime.utcnow().strftime("%d/%m/%Y") in after
    assert "____" not in after.split("DESCARGO")[0]


def _document_text(client, order_id: int) -> str:
    resp = client.get(f"/api/v1/orders/{order_id}/document")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    return "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(resp.content)).pages
    )
