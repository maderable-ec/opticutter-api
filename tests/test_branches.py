"""Tests for multi-branch isolation.

Covers: branch CRUD (admin only), global read for admin and seller with the
``branchId`` filter, seller creation defaulting to their base branch with
``branchId`` override, operator isolation (bound to their branch), order branch
inheritance (immutable historical fact), and the per-branch analytics breakdown.

Conftest's ``client`` is authenticated as admin (its session header); to act as
branch staff, pass ``headers=`` per request (httpx gives precedence to the
explicit header over the session one).
"""

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

from .test_orders import _create_board, _create_client, _order_payload

_PWD = "supersecret123"
_MATRIZ = 1  # default branch seeded by conftest


def _staff_headers(client, db_session, role, email, branch_id):
    """Seeds a staff user (role + branch) and returns their Bearer headers."""
    svc = UserService(db_session)
    if svc.get_by_email(email) is None:
        svc.create(
            UserCreate(
                email=email,
                password=_PWD,
                role=role,
                full_name=email,
                branch_id=branch_id,
            )
        )
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": _PWD}
    ).json()["data"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _payload_no_branch(client_id, product_id, **kw):
    """``_order_payload`` without ``branchId`` (to exercise the branch default)."""
    payload = _order_payload(client_id, product_id, **kw)
    payload.pop("branchId", None)
    return payload


def _make_branch(client, code="NORTE", name="Sucursal Norte"):
    return client.post("/api/v1/branches/", json={"code": code, "name": name}).json()[
        "data"
    ]


def _mint_order(db_session, client_id, product_id, branch_id, width=600):
    payload = _order_payload(client_id, product_id, width=width)
    payload["branchId"] = branch_id
    return OrderService(db_session).create(OrderCreate.model_validate(payload))


# --------------------------------------------------------------- branches CRUD
def test_branch_crud_is_admin_only(client, db_session):
    seller = _staff_headers(client, db_session, "vendedor", "s@e.com", _MATRIZ)

    # Admin creates; the seller can't manage but can read (populate selectors).
    created = client.post("/api/v1/branches/", json={"code": "NORTE", "name": "Norte"})
    assert created.status_code == 201
    assert (
        client.post(
            "/api/v1/branches/", json={"code": "X", "name": "X"}, headers=seller
        ).status_code
        == 403
    )
    assert client.get("/api/v1/branches/", headers=seller).status_code == 200


def test_branch_duplicate_code_returns_409(client):
    client.post("/api/v1/branches/", json={"code": "DUP", "name": "Uno"})
    dup = client.post("/api/v1/branches/", json={"code": "DUP", "name": "Dos"})
    assert dup.status_code == 409


def test_printing_switches_default_on_and_are_editable(client):
    """A new branch prints both types until the admin says its shop has no printer."""
    created = _make_branch(client, code="IMPR", name="Sucursal Impresión")
    assert created["printLabelsEnabled"] is True
    assert created["printConsolidatedEnabled"] is True

    updated = client.put(
        f"/api/v1/branches/{created['id']}",
        json={
            "code": "IMPR",
            "name": "Sucursal Impresión",
            "printLabelsEnabled": False,
        },
    )
    assert updated.status_code == 200
    body = updated.json()["data"]
    assert body["printLabelsEnabled"] is False
    assert body["printConsolidatedEnabled"] is True  # independent switches


# ----------------------------------------------- global seller (read + create)
def test_seller_reads_all_branches_and_create_defaults_to_base(client, db_session):
    """The seller is global: sees all branches and creates in their own by default."""
    norte = _make_branch(client)
    a = _staff_headers(client, db_session, "vendedor", "a@e.com", _MATRIZ)
    b = _staff_headers(client, db_session, "vendedor", "b@e.com", norte["id"])
    c = _create_client(client)
    board = _create_board(client)

    # Without branchId in the body, each seller falls back to their base branch.
    pre_a = client.post(
        "/api/v1/preorders/", json=_payload_no_branch(c["id"], board["id"]), headers=a
    ).json()["data"]
    pre_b = client.post(
        "/api/v1/preorders/", json=_payload_no_branch(c["id"], board["id"]), headers=b
    ).json()["data"]
    assert pre_a["branch"]["id"] == _MATRIZ
    assert pre_b["branch"]["id"] == norte["id"]

    # A (base Matriz) now SEES both and can access the one from another branch (no longer 404).
    a_ids = {
        p["id"] for p in client.get("/api/v1/preorders/", headers=a).json()["data"]
    }
    assert {pre_a["id"], pre_b["id"]} <= a_ids
    assert client.get(f"/api/v1/preorders/{pre_b['id']}", headers=a).status_code == 200

    # Can narrow with branchId (used to be admin-only).
    filtered = client.get(
        "/api/v1/preorders/", params={"branchId": norte["id"]}, headers=a
    ).json()["data"]
    assert [p["id"] for p in filtered] == [pre_b["id"]]


def test_seller_can_create_in_another_branch_with_branch_id(client, db_session):
    """The seller overrides their base branch by passing branchId on create."""
    norte = _make_branch(client)
    a = _staff_headers(client, db_session, "vendedor", "a@e.com", _MATRIZ)
    c = _create_client(client)
    board = _create_board(client)

    payload = _order_payload(c["id"], board["id"])
    payload["branchId"] = norte["id"]
    pre = client.post("/api/v1/preorders/", json=payload, headers=a).json()["data"]
    assert pre["branch"]["id"] == norte["id"]


def test_admin_must_specify_branch_on_preorder_create(client, db_session):
    """The admin has no base branch: creating without branchId is a 422."""
    c = _create_client(client)
    board = _create_board(client)
    # ``client`` is authenticated as admin (no branch): branchId is missing.
    resp = client.post(
        "/api/v1/preorders/", json=_payload_no_branch(c["id"], board["id"])
    )
    assert resp.status_code == 422


# ------------------------------------------------------------ orders isolation
def test_seller_sees_orders_across_branches(client, db_session):
    """The seller (global) sees and accesses orders from any branch."""
    norte = _make_branch(client)
    a = _staff_headers(client, db_session, "vendedor", "a@e.com", _MATRIZ)
    c = _create_client(client)
    board = _create_board(client)

    order_a = _mint_order(db_session, c["id"], board["id"], _MATRIZ, width=600)
    order_b = _mint_order(db_session, c["id"], board["id"], norte["id"], width=500)
    assert order_a.branch_id == _MATRIZ and order_b.branch_id == norte["id"]

    # A (base Matriz) sees BOTH and accesses the Norte one (no longer 404).
    a_ids = {o["id"] for o in client.get("/api/v1/orders/", headers=a).json()["data"]}
    assert {order_a.id, order_b.id} <= a_ids
    assert client.get(f"/api/v1/orders/{order_b.id}", headers=a).status_code == 200

    # The admin sees all; ``branchId`` narrows to one (same as the seller).
    assert len(client.get("/api/v1/orders/").json()["data"]) == 2
    only_norte = client.get(
        "/api/v1/orders/", params={"branchId": norte["id"]}, headers=a
    ).json()["data"]
    assert [o["id"] for o in only_norte] == [order_b.id]


def test_orders_isolated_for_operator(client, db_session):
    """The operator stays bound to their branch: can't see or access another's."""
    norte = _make_branch(client)
    op = _staff_headers(client, db_session, "operador", "op@e.com", _MATRIZ)
    c = _create_client(client)
    board = _create_board(client)

    order_a = _mint_order(db_session, c["id"], board["id"], _MATRIZ, width=600)
    order_b = _mint_order(db_session, c["id"], board["id"], norte["id"], width=500)

    op_ids = [o["id"] for o in client.get("/api/v1/orders/", headers=op).json()["data"]]
    assert op_ids == [order_a.id]
    assert client.get(f"/api/v1/orders/{order_b.id}", headers=op).status_code == 404
    assert client.get(f"/api/v1/orders/{order_a.id}", headers=op).status_code == 200


def test_reassigning_branch_changes_operator_visibility_not_history(client, db_session):
    """Moving an operator's branch changes what they see, not the orders' branch."""
    norte = _make_branch(client)
    op_email = "mover@e.com"
    op = _staff_headers(client, db_session, "operador", op_email, _MATRIZ)
    c = _create_client(client)
    board = _create_board(client)
    order_matriz = _mint_order(db_session, c["id"], board["id"], _MATRIZ)

    assert [
        o["id"] for o in client.get("/api/v1/orders/", headers=op).json()["data"]
    ] == [order_matriz.id]

    # The admin reassigns the operator to Norte; takes effect instantly (role/branch
    # are read from the DB on every request, not from the JWT).
    svc = UserService(db_session)
    user = svc.get_by_email(op_email)
    client.put(f"/api/v1/users/{user.id}", json={"branchId": norte["id"]})

    # No longer sees the Matriz order (it's still Matriz's: historical fact).
    assert client.get("/api/v1/orders/", headers=op).json()["data"] == []
    assert order_matriz.branch_id == _MATRIZ


def test_operator_without_branch_is_forbidden(client, db_session):
    """An operator with no assigned branch (invalid state) gets 403 when operating."""
    headers = _staff_headers(client, db_session, "operador", "huerfano@e.com", _MATRIZ)
    # The branch is removed directly in the DB (the API doesn't allow leaving it unset).
    svc = UserService(db_session)
    user = svc.get_by_email("huerfano@e.com")
    user.branch_id = None
    db_session.commit()
    assert client.get("/api/v1/orders/", headers=headers).status_code == 403


# ------------------------------------------------------------------- analytics
def test_analytics_breakdown_by_branch(client, db_session):
    norte = _make_branch(client)
    c = _create_client(client)
    board = _create_board(client)
    _mint_order(db_session, c["id"], board["id"], _MATRIZ, width=600)
    _mint_order(db_session, c["id"], board["id"], norte["id"], width=500)

    rng = {"from": "2020-01-01", "to": "2999-12-31"}
    items = client.get("/api/v1/analytics/breakdown/branch", params=rng).json()["data"][
        "items"
    ]
    by_label = {i["label"]: i for i in items}
    # Densifies all branches; each with its order count.
    assert by_label["Casa Matriz"]["orderCount"] == 1
    assert by_label["Sucursal Norte"]["orderCount"] == 1

    # The branchId filter narrows the summary to a single branch.
    only_norte = client.get(
        "/api/v1/analytics/summary", params={**rng, "branchId": norte["id"]}
    ).json()["data"]
    assert only_norte["orderCount"] == 1


def test_list_branches_filter_by_active_and_order(client):
    """A branch is retired by unsetting `isActive`, never deleted, so the inactive
    ones accumulate in the middle of a listing that could not filter them out."""
    norte = _make_branch(client, code="NORTE", name="Sucursal Norte")
    _make_branch(client, code="AUSTRO", name="Austro")
    client.put(f"/api/v1/branches/{norte['id']}", json={"isActive": False})

    # Omitting the flag keeps listing both: the admin manages retired branches.
    assert client.get("/api/v1/branches/").json()["meta"]["pagination"]["total"] == 3

    active = client.get("/api/v1/branches/", params={"isActive": True}).json()
    assert norte["id"] not in [b["id"] for b in active["data"]]
    inactive = client.get("/api/v1/branches/", params={"isActive": False}).json()
    assert [b["code"] for b in inactive["data"]] == ["NORTE"]

    # Ordered by name, with the primary key as the tiebreaker that makes paging safe.
    names = [b["name"] for b in client.get("/api/v1/branches/").json()["data"]]
    assert names == sorted(names)
