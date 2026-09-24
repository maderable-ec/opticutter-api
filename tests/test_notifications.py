"""Integration tests for the notifications module.

Order status transitions fan out notifications: ``-> finished`` notifies the
global admins/sellers; the real enqueue ``confirmed -> queued`` notifies the
operators of the order's branch. The acting user is excluded, the admin rollback
``in_process -> queued`` notifies nobody, and every recipient reads/acks only their
own notifications (scoped by ``current_user.id``).
"""

from src.modules.branches.model import BranchModel
from src.modules.notifications.model import NotificationModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.security import create_access_token
from tests.order_helpers import _patch_activity, _patch_status, _to_finished

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
    """``role`` is one role or a list of them."""
    roles = [role] if isinstance(role, str) else list(role)
    svc = UserService(db_session)
    user = svc.get_by_email(email)
    if user is None:
        user = svc.create(
            UserCreate(
                email=email,
                password=_PWD,
                roles=roles,
                full_name=email.split("@")[0].title(),
                branch_id=None if "administrador" in roles else branch_id,
            )
        )
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.roles)}"}


def _second_branch(db_session):
    branch = BranchModel(code="SUR", name="Sucursal Sur", is_active=True)
    db_session.add(branch)
    db_session.commit()
    return branch.id


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
# -> cancelled: the admins always, the branch operators only from the queue
# --------------------------------------------------------------------------- #
def test_cancelling_from_the_queue_reaches_admins_and_branch_operators(
    client, db_session
):
    order = _mint_order(client, db_session)
    branch2 = _second_branch(db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    op2 = _seed_user(db_session, "operador", "op2@empresa.com", branch_id=branch2)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")

    assert _patch_status(client, order["id"], "queued").status_code == 200
    # Clear the enqueue notification so what is left is only the cancellation.
    client.post("/api/v1/notifications/read-all", headers=_headers(op1))

    resp = _patch_status(client, order["id"], "cancelled", note="El cliente desistió")
    assert resp.status_code == 200

    # Two emissions, two disjoint audiences, one row each.
    for recipient in (admin, op1):
        items = _list(client, _headers(recipient), unread=True)
        assert [i["type"] for i in items] == ["order.cancelled"]
        assert items[0]["orderId"] == order["id"]
        assert items[0]["data"]["orderCode"] == order["code"]
        # The copy names the queue, because this order did leave a board.
        assert "cola de producción" in items[0]["body"]

    # Another branch's operator never had the card; a seller is not an admin.
    assert _unread_count(client, _headers(op2)) == 0
    assert _unread_count(client, _headers(seller)) == 0


def test_cancelling_a_confirmed_order_reaches_only_the_admins(client, db_session):
    """An order exists because a client confirmed a quote, so killing one is a sale
    that died -- but it never reached the shop, so no board lost a card."""
    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")

    assert (
        _patch_status(
            client, order["id"], "cancelled", note="Cotización descartada"
        ).status_code
        == 200
    )

    items = _list(client, _headers(admin), unread=True)
    assert [i["type"] for i in items] == ["order.cancelled"]
    # No board was involved, so the copy does not mention one.
    assert "cola" not in items[0]["body"]

    assert _unread_count(client, _headers(op1)) == 0
    assert _unread_count(client, _headers(seller)) == 0


def test_the_admin_who_cancels_is_not_notified_of_their_own_action(client, db_session):
    """The ``client`` fixture IS an admin: the actor is excluded even when that
    leaves the fan-out empty (it does NOT then fall back to anybody else)."""
    order = _mint_order(client, db_session)
    assert (
        _patch_status(client, order["id"], "cancelled", note="motivo").status_code
        == 200
    )
    assert db_session.query(NotificationModel).count() == 0


# --------------------------------------------------------------------------- #
# -> completed: admins/sellers, excluding the actor
# --------------------------------------------------------------------------- #
def test_finished_notifies_admins_sellers_excluding_actor(client, db_session):
    order = _mint_order(client, db_session)
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    other_admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    operator = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)

    _to_finished(client, order["id"])  # actor = Conftest Admin (the client fixture)

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
# in_process -> queued rollback: nobody is notified
# --------------------------------------------------------------------------- #
def test_a_bander_who_also_cuts_hears_the_queue_once(client, db_session):
    """Holding operador is what puts them in the branch operators' audience."""
    order = _mint_order(client, db_session)
    apprentice = _seed_user(
        db_session, ["canteador", "operador"], "aprendiz@empresa.com"
    )
    bander = _seed_user(db_session, "canteador", "canta@empresa.com")

    assert _patch_status(client, order["id"], "queued").status_code == 200

    items = _list(client, _headers(apprentice))
    assert [n["type"] for n in items] == ["order.queued"]
    # A bander alone is still not told: the queue is the cutters' news.
    assert _unread_count(client, _headers(bander)) == 0


def test_rollback_to_queued_does_not_notify(client, db_session):
    order = _mint_order(client, db_session)
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)

    assert _patch_status(client, order["id"], "queued").status_code == 200  # notifies
    assert (
        _patch_activity(client, order["id"], "cutting", "in_progress").status_code
        == 200
    )
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


# --------------------------------------------------------------------------- #
# The client confirms the quote: the seller who raised it is told
# --------------------------------------------------------------------------- #
def _preorder_for(client, db_session, headers, identifier, code):
    """Quote raised by the given staff member (that is what ``createdBy`` is)."""
    c = _create_client(client, identifier=identifier)
    b = _create_board(client, code=code)
    pre = client.post(
        "/api/v1/preorders/",
        json=_order_payload(c["id"], b["id"]),
        headers=headers,
    )
    assert pre.status_code == 201
    return pre.json()["data"]


def _confirm(client, pre_id, headers):
    token = client.post(
        f"/api/v1/preorders/{pre_id}/review-link", headers=headers
    ).json()["data"]["token"]
    resp = client.post(f"/api/v1/public/review/{token}/confirm")
    assert resp.status_code == 200
    return resp.json()["data"]


def test_client_confirmation_notifies_the_quote_owner(client, db_session):
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    other = _seed_user(db_session, "vendedor", "sell2@empresa.com")
    operator = _seed_user(db_session, "operador", "op1@empresa.com")
    headers = _headers(seller)
    pre = _preorder_for(client, db_session, headers, "0100000397", "MEL18")

    confirmed = _confirm(client, pre["id"], headers)

    items = _list(client, headers)
    assert [i["type"] for i in items] == ["order.confirmed"]
    assert items[0]["data"]["preorderCode"] == pre["code"]
    assert items[0]["data"]["orderCode"] == confirmed["orderCode"]
    assert items[0]["orderId"] is not None
    assert pre["code"] in items[0]["body"]

    # It is addressed to the owner: not to another seller, and not to the shop
    # floor (the order is born ``confirmed``, nothing is queued yet).
    assert _unread_count(client, _headers(other)) == 0
    assert _unread_count(client, _headers(operator)) == 0


def test_confirmation_falls_back_when_the_owner_is_inactive(client, db_session):
    """A closed sale must not be lost to a seller who left the company."""
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    headers = _headers(seller)
    pre = _preorder_for(client, db_session, headers, "0100000298", "MEL19")
    token = client.post(
        f"/api/v1/preorders/{pre['id']}/review-link", headers=headers
    ).json()["data"]["token"]
    # The seller leaves the company while the quote sits with the client.
    seller.is_active = False
    db_session.commit()

    assert client.post(f"/api/v1/public/review/{token}/confirm").status_code == 200

    # The office picks it up; the deactivated owner gets nothing (they cannot
    # even authenticate any more, so this is checked against the table).
    admin_items = _list(client, client.headers, unread=True)
    assert [i["type"] for i in admin_items] == ["order.confirmed"]
    assert (
        db_session.query(NotificationModel)
        .filter(NotificationModel.user_id == seller.id)
        .count()
        == 0
    )


def test_reconfirming_does_not_notify_twice(client, db_session):
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    headers = _headers(seller)
    pre = _preorder_for(client, db_session, headers, "0100000306", "MEL20")
    token = client.post(
        f"/api/v1/preorders/{pre['id']}/review-link", headers=headers
    ).json()["data"]["token"]

    for _ in range(2):  # a double click, or a retry after a dropped response
        assert client.post(f"/api/v1/public/review/{token}/confirm").status_code == 200

    assert _unread_count(client, headers) == 1


# --------------------------------------------------------------------------- #
# order.low_stock: the one event a live inventory reading decides
# --------------------------------------------------------------------------- #
def _stocked_branch(db_session, quantity):
    """Points conftest's default branch at a warehouse holding ``quantity``.

    The catalog product created by ``_create_board`` is hand-made and carries no
    ``external_code``, so it is invisible to the vendor — the test wires one on
    to make the order's board a SYNCED product, which is the only kind stock can
    be known for.
    """
    from src.modules.branches.model import BranchModel as _Branch
    from src.modules.inventory.external_inventory import StockRow
    from src.modules.products.model import ProductModel

    branch = db_session.get(_Branch, _BRANCH)
    branch.warehouse_code = 1
    product = (
        db_session.query(ProductModel)
        .filter(ProductModel.type == "board")
        .order_by(ProductModel.id)
        .first()
    )
    product.external_code = "TABLEROS:155"
    db_session.commit()
    return [StockRow(external_code="TABLEROS:155", warehouse_code=1, quantity=quantity)]


def _serve_stock(monkeypatch, rows):
    monkeypatch.setattr("src.modules.inventory.service.fetch_stock", lambda: list(rows))


def test_low_stock_reaches_the_admins_when_the_order_is_queued(
    client, db_session, monkeypatch
):
    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    op1 = _seed_user(db_session, "operador", "op1@empresa.com", branch_id=_BRANCH)
    _serve_stock(monkeypatch, _stocked_branch(db_session, quantity=1.0))

    assert _patch_status(client, order["id"], "queued").status_code == 200

    items = _list(client, _headers(admin))
    assert [i["type"] for i in items] == ["order.low_stock"]
    assert items[0]["orderId"] == order["id"]
    assert items[0]["data"]["event"] == "queued"
    assert "entró a la cola" in items[0]["body"]
    # The shop floor still gets its own, unrelated, "in the queue" notification —
    # and NOT the restocking one, which it cannot act on.
    assert [i["type"] for i in _list(client, _headers(op1))] == ["order.queued"]


def test_a_well_stocked_order_notifies_nobody_about_stock(
    client, db_session, monkeypatch
):
    """The half of the rule that matters: this is a purchasing signal, so an
    order that eats into nothing scarce must stay silent."""
    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    _serve_stock(monkeypatch, _stocked_branch(db_session, quantity=500.0))

    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _unread_count(client, _headers(admin)) == 0


def test_a_branch_without_a_warehouse_notifies_nobody_about_stock(
    client, db_session, monkeypatch
):
    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    rows = _stocked_branch(db_session, quantity=1.0)
    branch = db_session.get(BranchModel, _BRANCH)
    branch.warehouse_code = None
    db_session.commit()
    _serve_stock(monkeypatch, rows)

    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _unread_count(client, _headers(admin)) == 0


def test_an_unreachable_vendor_never_blocks_the_enqueue(
    client, db_session, monkeypatch
):
    """The payment is already committed by the time this runs: a third party's
    database being down cannot be allowed to undo it."""
    from src.shared.exceptions import ExternalServiceError

    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    _stocked_branch(db_session, quantity=1.0)

    def _down():
        raise ExternalServiceError("SIFAC caída")

    monkeypatch.setattr("src.modules.inventory.service.fetch_stock", _down)

    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _unread_count(client, _headers(admin)) == 0


def test_the_rollback_does_not_re_announce_low_stock(client, db_session, monkeypatch):
    """``in_process -> queued`` is not a sale reaching the queue, so it is not a
    restocking moment either."""
    order = _mint_order(client, db_session)
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    _serve_stock(monkeypatch, _stocked_branch(db_session, quantity=1.0))

    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert (
        _patch_activity(client, order["id"], "cutting", "in_progress").status_code
        == 200
    )
    client.post("/api/v1/notifications/read-all", headers=_headers(admin))

    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _unread_count(client, _headers(admin)) == 0


def test_low_stock_reaches_the_admins_when_the_order_is_born(
    client, db_session, monkeypatch
):
    """The confirm is the only door an order comes through — there is no
    ``POST /orders`` — so it is the only place the "created" half can fire."""
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    headers = _headers(seller)
    pre = _preorder_for(client, db_session, headers, "0100000397", "MEL18")
    _serve_stock(monkeypatch, _stocked_branch(db_session, quantity=1.0))

    _confirm(client, pre["id"], headers)

    # The seller hears the sale closed; the admin hears what has to be restocked.
    assert [i["type"] for i in _list(client, headers)] == ["order.confirmed"]
    items = _list(client, _headers(admin))
    assert [i["type"] for i in items] == ["order.low_stock"]
    assert items[0]["data"]["event"] == "created"
    assert "se confirmó" in items[0]["body"]


def test_a_well_stocked_new_order_notifies_no_admin(client, db_session, monkeypatch):
    seller = _seed_user(db_session, "vendedor", "sell@empresa.com")
    admin = _seed_user(db_session, "administrador", "admin2@empresa.com")
    headers = _headers(seller)
    pre = _preorder_for(client, db_session, headers, "0100000397", "MEL18")
    _serve_stock(monkeypatch, _stocked_branch(db_session, quantity=500.0))

    _confirm(client, pre["id"], headers)

    assert _unread_count(client, _headers(admin)) == 0
    assert _unread_count(client, headers) == 1  # the sale itself still lands
