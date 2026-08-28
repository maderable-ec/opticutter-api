"""Integration tests for the notifications module.

Order status transitions fan out notifications: ``-> completed`` notifies the
global admins/sellers; the real enqueue ``confirmed -> queued`` notifies the
operators of the order's branch. The acting user is excluded, the admin rollback
``cutting -> queued`` notifies nobody, and every recipient reads/acks only their
own notifications (scoped by ``current_user.id``).
"""

from src.modules.branches.model import BranchModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.security import create_access_token

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
    """Creates an order via the service (born ``confirmed``); reads it back via GET."""
    c = _create_client(client, identifier=identifier)
    b = _create_board(client, code=code)
    order = OrderService(db_session).create(
        OrderCreate.model_validate(_order_payload(c["id"], b["id"], width=width))
    )
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def _seed_user(db_session, role, email, branch_id=_BRANCH):
    svc = UserService(db_session)
    user = svc.get_by_email(email)
    if user is None:
        user = svc.create(
            UserCreate(
                email=email,
                password=_PWD,
                role=role,
                full_name=email.split("@")[0].title(),
                branch_id=None if role == "administrador" else branch_id,
            )
        )
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


def _second_branch(db_session):
    branch = BranchModel(code="SUR", name="Sucursal Sur", is_active=True)
    db_session.add(branch)
    db_session.commit()
    return branch.id


def _patch_status(client, oid, status, **kw):
    body = {"status": status}
    if status == "queued":
        body["payment"] = {"cashAmount": 100.0}
    return client.patch(f"/api/v1/orders/{oid}/status", json=body, **kw)


def _to_completed(client, oid):
    """Drives the order (no edge banding) up to 'completed' as the admin client."""
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


def _unread_count(client, headers):
    return client.get("/api/v1/notifications/unread-count", headers=headers).json()[
        "data"
    ]["count"]


def _list(client, headers, **params):
    return client.get("/api/v1/notifications/", headers=headers, params=params).json()[
        "data"
    ]


# --------------------------------------------------------------------------- #
# confirmed -> queued: only the branch operators
# --------------------------------------------------------------------------- #
def test_queued_notifies_only_the_order_branch_operators(client, db_session):
    order = _mint_order(client, db_session)
    branch2 = _second_branch(db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    op2 = _seed_user(db_session, "operador", "op2@empresa.com", branch_id=branch2)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")

    assert _patch_status(client, order["id"], "queued").status_code == 200

    # The branch-1 operator gets exactly one queued notification.
    assert _unread_count(client, _headers(op1)) == 1
    items = _list(client, _headers(op1))
    assert len(items) == 1
    assert items[0]["type"] == "order.queued"
    assert items[0]["orderId"] == order["id"]
    assert items[0]["data"]["orderCode"] == order["code"]
    assert items[0]["readAt"] is None

    # An operator of another branch and a (global) seller are not notified.
    assert _unread_count(client, _headers(op2)) == 0
    assert _unread_count(client, _headers(seller)) == 0


# --------------------------------------------------------------------------- #
# -> completed: admins/sellers, excluding the actor
# --------------------------------------------------------------------------- #
def test_completed_notifies_admins_sellers_excluding_actor(client, db_session):
    order = _mint_order(client, db_session)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    other_admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    operator = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)

    _to_completed(client, order["id"])  # actor = Conftest Admin (the client fixture)

    # The seller and a second admin receive the completed notification.
    for recipient in (seller, other_admin):
        items = _list(client, _headers(recipient), unread=True)
        assert [i["type"] for i in items] == ["order.completed"]
        assert items[0]["orderId"] == order["id"]

    # The acting admin is excluded and never got the queued one either.
    assert _unread_count(client, client.headers) == 0

    # The operator only got the enqueue notification, not the completion.
    op_items = _list(client, _headers(operator))
    assert [i["type"] for i in op_items] == ["order.queued"]


# --------------------------------------------------------------------------- #
# cutting -> queued rollback: nobody is notified
# --------------------------------------------------------------------------- #
def test_cutting_to_queued_rollback_does_not_notify(client, db_session):
    order = _mint_order(client, db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)

    assert _patch_status(client, order["id"], "queued").status_code == 200  # notifies
    assert _patch_status(client, order["id"], "cutting").status_code == 200
    assert _patch_status(client, order["id"], "queued").status_code == 200  # rollback

    # Still only the single notification from the real enqueue.
    assert _unread_count(client, _headers(op1)) == 1


# --------------------------------------------------------------------------- #
# read marking (scoped to the owner)
# --------------------------------------------------------------------------- #
def test_mark_read_clears_the_badge(client, db_session):
    order = _mint_order(client, db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    _patch_status(client, order["id"], "queued")

    headers = _headers(op1)
    notif = _list(client, headers)[0]
    resp = client.patch(f"/api/v1/notifications/{notif['id']}/read", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["readAt"] is not None
    assert _unread_count(client, headers) == 0


def test_mark_all_read_returns_updated_count(client, db_session):
    order = _mint_order(client, db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    _patch_status(client, order["id"], "queued")

    headers = _headers(op1)
    resp = client.post("/api/v1/notifications/read-all", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1
    assert _unread_count(client, headers) == 0


def test_cannot_mark_another_users_notification(client, db_session):
    order = _mint_order(client, db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    _patch_status(client, order["id"], "queued")

    notif = _list(client, _headers(op1))[0]
    # A different user gets a uniform 404 (never sees it isn't theirs).
    resp = client.patch(
        f"/api/v1/notifications/{notif['id']}/read", headers=_headers(seller)
    )
    assert resp.status_code == 404


def test_notifications_require_authentication(anon_client):
    assert anon_client.get("/api/v1/notifications/").status_code == 401
