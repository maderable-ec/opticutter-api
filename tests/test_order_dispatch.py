"""Tests for the 'despachado' (delivered to client) status and the dispatch sheet.

Dispatch is the real close of the cycle: ``completed → despachado`` (terminal). It is
a commercial act restricted to admin/seller by ``TRANSITION_ROLES`` — the shop floor
(operador/canteador) cannot register it. The dispatch sheet is a PDF with the pieces
(no prices), the liability disclaimer, and the signature lines.
"""

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

_PWD = "pw-supersecret"
_BRANCH = 1  # default branch seeded by conftest


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


def _patch_status(client, oid, status, **kw):
    body = {"status": status}
    if status == "queued":
        # Moving to the queue requires recording the (informational) payment method.
        body["payment"] = {"cashAmount": 100.0}
    return client.patch(f"/api/v1/orders/{oid}/status", json=body, **kw)


def _to_completed(client, oid):
    """Drives the order (no edge banding) up to 'completed' as admin."""
    assert _patch_status(client, oid, "queued").status_code == 200
    assert _patch_status(client, oid, "cutting").status_code == 200
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    for board in plan["boards"]:
        for piece in board["pieces"]:
            client.patch(
                f"/api/v1/orders/{oid}/cutting-plan/pieces/{piece['id']}",
                json={"cut": True},
            )
    assert _patch_status(client, oid, "cut").status_code == 200
    assert _patch_status(client, oid, "completed").status_code == 200


def _token_for(client, db_session, role, branch_id=_BRANCH, email=None):
    """Seeds a user with the given role and returns a Bearer header (real login)."""
    email = email or f"{role}@empresa.com"
    svc = UserService(db_session)
    if svc.get_by_email(email) is None:
        svc.create(
            UserCreate(
                email=email,
                password=_PWD,
                role=role,
                full_name=role.title(),
                branch_id=None if role == "administrador" else branch_id,
            )
        )
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": _PWD}
    ).json()["data"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Transition to 'despachado'
# --------------------------------------------------------------------------- #
def test_dispatch_from_completed_freezes_metadata(client, db_session):
    order = _mint_order(client, db_session)
    _to_completed(client, order["id"])

    resp = _patch_status(client, order["id"], "despachado")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "despachado"
    # Dispatch freezes date and the responsible user (shown on the dispatch sheet).
    assert data["dispatchedAt"] is not None
    assert data["dispatchedByLabel"] == "Conftest Admin"
    # History: the last entry records completed → despachado.
    assert data["history"][-1]["fromStatus"] == "completed"
    assert data["history"][-1]["toStatus"] == "despachado"


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
        _to_completed(client, order["id"])
        headers = _token_for(client, db_session, role)
        resp = _patch_status(client, order["id"], "despachado", headers=headers)
        assert resp.status_code == 403, role
        # The order stays completed: the rejected transition doesn't advance it.
        assert (
            client.get(f"/api/v1/orders/{order['id']}").json()["data"]["status"]
            == "completed"
        )


def test_seller_can_dispatch(client, db_session):
    """The seller (a commercial role) can register the dispatch."""
    order = _mint_order(
        client, db_session, identifier="0100000363", code="MELS0", width=550
    )
    _to_completed(client, order["id"])
    headers = _token_for(client, db_session, "vendedor")
    resp = _patch_status(client, order["id"], "despachado", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "despachado"


def test_dispatched_is_terminal(client, db_session):
    order = _mint_order(client, db_session)
    _to_completed(client, order["id"])
    assert _patch_status(client, order["id"], "despachado").status_code == 200

    # No outgoing transitions: anything after this is invalid.
    bad = _patch_status(client, order["id"], "cancelled")
    assert bad.status_code == 422
    assert "inválida" in bad.json()["errors"][0]["message"]


def test_cannot_dispatch_before_completed(client, db_session):
    """Only completed orders can be dispatched: from 'confirmed' the transition is invalid."""
    order = _mint_order(client, db_session)
    bad = _patch_status(client, order["id"], "despachado")
    assert bad.status_code == 422
    assert "inválida" in bad.json()["errors"][0]["message"]


# --------------------------------------------------------------------------- #
# Dispatch sheet (PDF)
# --------------------------------------------------------------------------- #
def test_dispatch_sheet_pdf_and_base64(client, db_session):
    order = _mint_order(client, db_session)
    _to_completed(client, order["id"])
    _patch_status(client, order["id"], "despachado")
    oid = order["id"]

    pdf = client.get(f"/api/v1/orders/{oid}/dispatch-sheet")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 1000

    b64 = client.get(
        f"/api/v1/orders/{oid}/dispatch-sheet", params={"format": "base64"}
    )
    assert b64.status_code == 200
    body = b64.json()
    assert body["format"] == "base64"
    assert body["mimeType"] == "application/pdf"
    assert order["code"] in body["filename"]


def test_dispatch_sheet_renders_before_dispatch(client, db_session):
    """The sheet can be issued even before dispatch (date falls back to 'today')."""
    order = _mint_order(client, db_session)
    sheet = client.get(f"/api/v1/orders/{order['id']}/dispatch-sheet")
    assert sheet.status_code == 200
    assert sheet.headers["content-type"] == "application/pdf"
    assert len(sheet.content) > 1000


def test_dispatch_sheet_404(client):
    assert client.get("/api/v1/orders/999999/dispatch-sheet").status_code == 404
