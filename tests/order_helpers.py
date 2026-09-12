"""Shared helpers to build an order and walk it through the shop floor.

Extracted because five test modules were importing them from
``test_order_banding`` and three more had their own copy of the same ladder up to
a closed order -- so a change to the state machine was paid four to five times,
in copies that had already started to drift.

Nothing here asserts anything about the machine itself: the helpers only DRIVE
it (and assert the drive worked, so a failure points at the setup rather than at
the assertion under test). The rules live in ``test_order_activities.py`` and
``tests/unit/test_order_state_machine.py``.
"""

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

_PWD = "pw-supersecret"
_BRANCH = 1  # default branch seeded by conftest


# --------------------------------------------------------------------------- #
# Catalog / order fixtures (same pattern as test_edge_banding.py)
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


def _order_mixed_pieces(client, db_session, identifier="0100000397"):
    """Order with TWO banded pieces and TWO plain ones, on a single board.

    The shape the banding floors are actually about: the plain pieces must never
    hold the bander back, and the banded ones must all be cut before finishing.
    """
    c = _create_client(client, identifier=identifier)
    suffix = identifier[-4:]
    b = _create_board(client, code=f"MEL{suffix}")
    eb = _create_edge_banding(client, code=f"TAP{suffix}")
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
                    "height": 500,
                    "width": 1000,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Costado",
                    "canRotate": True,
                    "edgeBanding": {"productId": eb["id"], "sides": ["top", "bottom"]},
                },
                {
                    "priority": 0,
                    "height": 400,
                    "width": 600,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Fondo",
                    "canRotate": True,
                },
            ],
        },
    )


def _order_without_banding(client, db_session, identifier="0100000397"):
    c = _create_client(client, identifier=identifier)
    suffix = identifier[-4:]
    b = _create_board(client, code=f"MEL{suffix}")
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


def _order_with_services(client, db_session, identifier="0100000397"):
    """Order with edge banding AND an additional service: all three activities."""
    c = _create_client(client, identifier=identifier)
    suffix = identifier[-4:]
    b = _create_board(client, code=f"MEL{suffix}")
    eb = _create_edge_banding(client, code=f"TAP{suffix}")
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
                    "height": 500,
                    "width": 1000,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Costado",
                    "canRotate": True,
                    "edgeBanding": {"productId": eb["id"], "sides": ["top", "bottom"]},
                },
                {
                    "priority": 0,
                    "height": 400,
                    "width": 600,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Fondo",
                    "canRotate": True,
                },
            ],
            "additionalServices": [
                {"name": "Perforación", "unitPrice": 5.0, "quantity": 1}
            ],
        },
    )


# --------------------------------------------------------------------------- #
# Driving the machine
# --------------------------------------------------------------------------- #
def _patch_status(client, oid, status, **kw):
    body = {"status": status}
    if status == "queued":
        # Moving to the queue requires registering the payment method (informational).
        body["payment"] = {"cashAmount": 100.0}
    return client.patch(f"/api/v1/orders/{oid}/status", json=body, **kw)


def _patch_activity(client, oid, activity, status, **kw):
    return client.patch(
        f"/api/v1/orders/{oid}/activities/{activity}", json={"status": status}, **kw
    )


def _activity(order_or_item, activity):
    """One entry of an ``activities`` list, or ``None`` when it does not apply."""
    return next((a for a in order_or_item["activities"] if a["type"] == activity), None)


def _to_in_process(client, oid):
    """Pays the order and has the shop start the cut, which is what moves it."""
    assert _patch_status(client, oid, "queued").status_code == 200
    assert _patch_activity(client, oid, "cutting", "in_progress").status_code == 200


def _to_finished(client, oid):
    """The full shop-floor ladder: cut everything, close every activity.

    The order finishes ITSELF when the last activity closes, so there is no
    status call at the end.
    """
    _to_in_process(client, oid)
    _cut_all_pieces(client, oid)
    order = client.get(f"/api/v1/orders/{oid}").json()["data"]
    # Banding before the cut: closing the cut may be the last activity, and once
    # the order is ``finished`` nothing else can be registered.
    for activity in ("banding", "additional"):
        if _activity(order, activity) is None:
            continue
        assert _patch_activity(client, oid, activity, "in_progress").status_code == 200
        assert _patch_activity(client, oid, activity, "done").status_code == 200
    assert _patch_activity(client, oid, "cutting", "done").status_code == 200


def _cut_all_pieces(client, oid):
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    for board in plan["boards"]:
        for piece in board["pieces"]:
            client.patch(
                f"/api/v1/orders/{oid}/cutting-plan/pieces/{piece['id']}",
                json={"cut": True},
            )


def _banded_pieces(client, oid):
    """Placed pieces carrying edge banding, in cutting-plan order."""
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    return [p for board in plan["boards"] for p in board["pieces"] if p["edges"]]


def _cut_piece(client, oid, piece, cut=True):
    return client.patch(
        f"/api/v1/orders/{oid}/cutting-plan/pieces/{piece['id']}",
        json={"cut": cut},
    )


def _cut_banded_pieces(client, oid):
    """Cuts every banded piece: what unlocks FINISHING the banding."""
    pieces = _banded_pieces(client, oid)
    for piece in pieces:
        assert _cut_piece(client, oid, piece).status_code == 200
    return len(pieces)


def _cut_first_banded_piece(client, oid):
    """Cuts a single banded piece: what unlocks STARTING the banding."""
    assert _cut_piece(client, oid, _banded_pieces(client, oid)[0]).status_code == 200


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
