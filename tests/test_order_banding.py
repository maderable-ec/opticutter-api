"""Tests for the PARALLEL edge-banding track: initial state, overlap with
cutting, completion gate (incl. completion by the shop floor), guards,
idempotency and canteador role RBAC. The workshop board that lists these orders
is covered in ``test_order_workshop_queue.py``."""

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

_PWD = "pw-supersecret"
_BRANCH = 1  # default branch seeded by conftest


# --------------------------------------------------------------------------- #
# Catalog / order helpers (same pattern as test_edge_banding.py)
# --------------------------------------------------------------------------- #
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


def _create_edge_banding(client, code="TAP22", price=2.0):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": code,
            "name": f"Tapacanto {code}",
            "price": price,
            "attributes": {
                "thickness": 0.45,
                "width": 22,
                "color": "Blanco",
                "length": 50000,
                "bandType": "Suave",
            },
        },
    ).json()["data"]


def _mint_order(client, db_session, payload):
    """Mints via the service (HTTP creation was removed) and reads it back via GET."""
    order = OrderService(db_session).create(OrderCreate.model_validate(payload))
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def _order_with_banding(client, db_session, branch_id=_BRANCH, identifier="0100000397"):
    c = _create_client(client, identifier=identifier)
    suffix = identifier[-4:]  # unique product codes per order (avoids 409)
    b = _create_board(client, code=f"MEL{suffix}")
    eb = _create_edge_banding(client, code=f"TAP{suffix}")
    return _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": branch_id,
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
        },
    )


def _order_without_banding(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    return _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [
                {
                    "priority": 0,
                    "height": 400,
                    "width": 600,
                    "quantity": 1,
                    "materialKey": "b1",
                    "label": "Puerta",
                    "canRotate": True,
                }
            ],
        },
    )


def _patch_status(client, oid, status, **kw):
    body = {"status": status}
    if status == "queued":
        # Moving to the queue requires registering the payment method (informational).
        body["payment"] = {"cashAmount": 100.0}
    return client.patch(f"/api/v1/orders/{oid}/status", json=body, **kw)


def _patch_banding(client, oid, status, **kw):
    return client.patch(f"/api/v1/orders/{oid}/banding", json={"status": status}, **kw)


def _to_cutting(client, oid):
    assert _patch_status(client, oid, "queued").status_code == 200
    assert _patch_status(client, oid, "cutting").status_code == 200


def _cut_all_pieces(client, oid):
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    for board in plan["boards"]:
        for piece in board["pieces"]:
            client.patch(
                f"/api/v1/orders/{oid}/cutting-plan/pieces/{piece['id']}",
                json={"cut": True},
            )


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
# Initial state of the edge-banding track
# --------------------------------------------------------------------------- #
def test_order_with_edge_banding_starts_pending(client, db_session):
    order = _order_with_banding(client, db_session)
    assert order["bandingStatus"] == "pending"
    assert order["bandingStartedAt"] is None
    assert order["bandingFinishedAt"] is None


def test_order_without_edge_banding_is_not_applicable(client, db_session):
    order = _order_without_banding(client, db_session)
    assert order["bandingStatus"] == "not_applicable"


# --------------------------------------------------------------------------- #
# Overlap: banding while cutting is still open
# --------------------------------------------------------------------------- #
def test_banding_runs_in_parallel_with_cutting(client, db_session):
    """Banding starts while the order is still 'cutting' (without closing the cut)."""
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])

    started = _patch_banding(client, order["id"], "in_progress")
    assert started.status_code == 200
    data = started.json()["data"]
    assert data["bandingStatus"] == "in_progress"
    assert data["bandingStartedAt"] is not None

    # The order stays in cutting and the cutting plan can keep being marked in parallel.
    detail = client.get(f"/api/v1/orders/{order['id']}").json()["data"]
    assert detail["status"] == "cutting"
    assert detail["bandingStartedByLabel"]  # frozen actor


def test_banding_finish_then_order_completes(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    assert _patch_banding(client, order["id"], "in_progress").status_code == 200
    finished = _patch_banding(client, order["id"], "done")
    assert finished.status_code == 200
    assert finished.json()["data"]["bandingFinishedAt"] is not None

    # Close the cut and complete: with banding finished, the gate passes.
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut").status_code == 200
    assert _patch_status(client, order["id"], "completed").status_code == 200


# --------------------------------------------------------------------------- #
# Completion gate: only if the order has edge banding
# --------------------------------------------------------------------------- #
def test_complete_blocked_until_banding_done(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut").status_code == 200

    # Banding still pending → can't complete.
    blocked = _patch_status(client, order["id"], "completed")
    assert blocked.status_code == 422
    assert "canteado" in blocked.json()["errors"][0]["message"].lower()

    # After finishing banding (valid in 'cut' state), completion passes.
    assert _patch_banding(client, order["id"], "in_progress").status_code == 200
    assert _patch_banding(client, order["id"], "done").status_code == 200
    assert _patch_status(client, order["id"], "completed").status_code == 200


def test_complete_without_edge_banding_needs_no_banding(client, db_session):
    """An order without edge banding (not_applicable) completes without a banding step."""
    order = _order_without_banding(client, db_session)
    _to_cutting(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut").status_code == 200
    assert _patch_status(client, order["id"], "completed").status_code == 200


# --------------------------------------------------------------------------- #
# Banding transition guards
# --------------------------------------------------------------------------- #
def test_banding_requires_order_in_cutting_or_cut(client, db_session):
    """Before cutting (queued) there are no released pieces to band → 422."""
    order = _order_with_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200
    resp = _patch_banding(client, order["id"], "in_progress")
    assert resp.status_code == 422
    assert "corte" in resp.json()["errors"][0]["message"].lower()


def test_banding_not_applicable_order_rejected(client, db_session):
    """An order without edge banding doesn't accept banding registration → 422."""
    order = _order_without_banding(client, db_session)
    _to_cutting(client, order["id"])
    resp = _patch_banding(client, order["id"], "in_progress")
    assert resp.status_code == 422
    assert "tapacantos" in resp.json()["errors"][0]["message"].lower()


def test_banding_invalid_transition_skipping_in_progress(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    resp = _patch_banding(client, order["id"], "done")  # pending → done (skips start)
    assert resp.status_code == 422
    assert "inválida" in resp.json()["errors"][0]["message"].lower()


def test_banding_in_progress_is_idempotent(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    first = _patch_banding(client, order["id"], "in_progress").json()["data"]
    again = _patch_banding(client, order["id"], "in_progress").json()["data"]
    # Re-applying doesn't re-stamp the start time.
    assert again["bandingStartedAt"] == first["bandingStartedAt"]
    assert again["bandingStatus"] == "in_progress"


# --------------------------------------------------------------------------- #
# canteador role RBAC
# --------------------------------------------------------------------------- #
def test_canteador_can_band_but_not_read_order_detail(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    headers = _token_for(client, db_session, "canteador")

    # Can register banding and see their workshop board...
    assert (
        _patch_banding(client, order["id"], "in_progress", headers=headers).status_code
        == 200
    )
    assert (
        client.get("/api/v1/orders/workshop-queue", headers=headers).status_code == 200
    )
    # ...but NOT the order detail (no orders:read).
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=headers).status_code == 403
    )


def test_operator_and_seller_cannot_band(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    for role in ("operador", "vendedor"):
        headers = _token_for(client, db_session, role)
        resp = _patch_banding(client, order["id"], "in_progress", headers=headers)
        assert resp.status_code == 403, role


# --------------------------------------------------------------------------- #
# Completion by the shop floor (operador / canteador)
# --------------------------------------------------------------------------- #
def test_operator_completes_order_without_banding(client, db_session):
    """Scenario 3: the operator cuts and completes a no-banding order end to end."""
    order = _order_without_banding(client, db_session)
    op = _token_for(client, db_session, "operador")
    _to_cutting(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut", headers=op).status_code == 200
    assert (
        _patch_status(client, order["id"], "completed", headers=op).status_code == 200
    )


def test_canteador_completes_after_finishing_banding(client, db_session):
    """Scenario 1: the operator moves on to another order; the canteador completes
    this one only after finishing the banding (Gate B blocks it before)."""
    order = _order_with_banding(client, db_session)
    _to_cutting(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut").status_code == 200

    canteador = _token_for(client, db_session, "canteador")
    # Blocked while banding is unfinished, even for the canteador.
    blocked = _patch_status(client, order["id"], "completed", headers=canteador)
    assert blocked.status_code == 422
    assert "canteado" in blocked.json()["errors"][0]["message"].lower()

    # After finishing banding, the canteador completes it from their own token.
    assert (
        _patch_banding(
            client, order["id"], "in_progress", headers=canteador
        ).status_code
        == 200
    )
    assert (
        _patch_banding(client, order["id"], "done", headers=canteador).status_code
        == 200
    )
    assert (
        _patch_status(client, order["id"], "completed", headers=canteador).status_code
        == 200
    )
