"""Tests for additional services (servicios adicionales).

Covers: the pure ``build_pricing`` fold (services registered tax-included and
converted to net so one tax line covers them), the catalog CRUD + RBAC (admin
writes, seller only reads), the pre-order (quote) selection, the freeze into the
order, and the public review projection.
"""

from src.modules.optimizations.pricing import build_pricing
from src.modules.orders.model import OrderModel
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.security import create_access_token

from .test_orders import _create_board, _create_client, _mint_order, _order_payload


def _service_line(service_id=None, name="Perforación", unit_price=2.0, quantity=3):
    line = {"name": name, "unitPrice": unit_price, "quantity": quantity}
    if service_id is not None:
        line["serviceId"] = service_id
    return line


# --- build_pricing (pure layer) -----------------------------------------------
def _payload(*, catalog_boards=100.0, edge=0.0):
    return {
        "total_boards_cost": catalog_boards,
        "total_edge_banding_cost": edge,
    }


def test_build_pricing_folds_services_into_the_taxed_subtotal():
    # Registered tax-included, so each line converts: (2*3)/1.15 = 5.22 and
    # 15/1.15 = 13.04. They join the subtotal instead of being added after the
    # tax, or "Subtotal + IVA" wouldn't add up to the total on the page.
    services = [{"unit_price": 2.0, "quantity": 3}, {"unit_price": 15.0, "quantity": 1}]
    p = build_pricing(_payload(catalog_boards=100.0, edge=20.0), 1, services, 0.15)
    assert p["services_total"] == 18.26
    assert p["subtotal"] == 138.26
    assert p["tax_amount"] == 20.74
    assert p["total"] == 159.0


def test_build_pricing_without_services_is_unchanged():
    p = build_pricing(_payload(catalog_boards=100.0, edge=20.0), 1, None, 0.15)
    assert p["services_total"] == 0.0
    assert p["subtotal"] == 120.0
    assert p["total"] == 138.0


# --- Catalog CRUD + RBAC ------------------------------------------------------
def _create_service(client, name="Perforación", price=2.0, is_active=True):
    return client.post(
        "/api/v1/additional-services/",
        json={"name": name, "price": price, "isActive": is_active},
    )


def test_create_and_get_additional_service(client):
    resp = _create_service(client)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["name"] == "Perforación"
    assert data["price"] == 2.0
    assert data["isActive"] is True

    got = client.get(f"/api/v1/additional-services/{data['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["id"] == data["id"]


def test_list_filters_by_active(client):
    _create_service(client, name="Activo", is_active=True)
    _create_service(client, name="Inactivo", is_active=False)

    active = client.get("/api/v1/additional-services/?isActive=true").json()["data"]
    names = {s["name"] for s in active}
    assert "Activo" in names
    assert "Inactivo" not in names


def test_duplicate_name_conflicts(client):
    _create_service(client, name="Armado")
    dup = _create_service(client, name="Armado")
    assert dup.status_code == 409


def test_seller_reads_but_cannot_write(client, db_session):
    _create_service(client, name="Perforación")
    seller = UserService(db_session).create(
        UserCreate(
            email="seller-services@empresa.com",
            password="seller-password",
            role="vendedor",
            full_name="Seller",
            branch_id=1,
        )
    )
    admin_auth = client.headers["Authorization"]
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(seller.id, seller.role)}"
    )
    try:
        assert client.get("/api/v1/additional-services/").status_code == 200
        blocked = _create_service(client, name="Nuevo")
        assert blocked.status_code == 403
    finally:
        client.headers["Authorization"] = admin_auth


# --- Pre-order (quote): live selection ----------------------------------------
def test_preorder_with_services_folds_them_into_total(client):
    c = _create_client(client)
    b = _create_board(client)  # price 45.5, 1 board used
    svc = _create_service(client).json()["data"]

    payload = _order_payload(c["id"], b["id"])
    payload["additionalServices"] = [_service_line(service_id=svc["id"])]

    resp = client.post("/api/v1/preorders/", json=payload)
    assert resp.status_code == 201
    data = resp.json()["data"]

    assert len(data["additionalServices"]) == 1
    assert data["additionalServices"][0]["unitPrice"] == 2.0
    pricing = data["optimization"]["pricing"]
    # $2.00 x 3 is registered tax-included, so it lands net in the subtotal:
    # 6.00 / 1.15 = 5.22. The tax line then puts the 15% back, and the client
    # still pays the $6.00 the seller quoted for the service.
    assert pricing["servicesTotal"] == 5.22
    assert pricing["subtotal"] == 50.72  # 45.50 board + 5.22 service
    assert pricing["taxAmount"] == 7.61
    assert pricing["total"] == 58.33

    # The proforma renders with the services section (no exception).
    pdf = client.get(f"/api/v1/preorders/{data['id']}/proforma")
    assert pdf.status_code == 200


def test_preorder_update_edits_services(client):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["additionalServices"] = [_service_line(unit_price=2.0, quantity=3)]
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]

    # Edit the unit price (the whole point: editable per line).
    updated = client.put(
        f"/api/v1/preorders/{pre['id']}",
        json={"additionalServices": [_service_line(unit_price=5.0, quantity=2)]},
    ).json()["data"]
    assert updated["optimization"]["pricing"]["servicesTotal"] == 8.7  # 10.00/1.15
    assert updated["optimization"]["pricing"]["total"] == 62.33


# --- Order: freeze ------------------------------------------------------------
def test_order_freezes_services(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["additionalServices"] = [_service_line(unit_price=2.0, quantity=3)]

    order = _mint_order(db_session, payload)
    data = client.get(f"/api/v1/orders/{order.id}").json()["data"]

    assert data["subtotal"] == 50.72
    assert data["additionalServicesTotal"] == 5.22  # frozen NET
    assert data["total"] == 58.33
    assert len(data["additionalServices"]) == 1
    assert data["additionalServices"][0]["name"] == "Perforación"

    # Frozen against later edits: the column holds the total.
    db_session.expire_all()
    frozen = db_session.get(OrderModel, order.id)
    assert frozen.additional_services_total == 5.22
    assert frozen.total == 58.33


def test_dedupe_distinguishes_services(client, db_session):
    from src.modules.orders.schemas import OrderCreate
    from src.modules.orders.service import OrderService

    c = _create_client(client)
    b = _create_board(client)
    svc = OrderService(db_session)
    base = _order_payload(c["id"], b["id"])

    plain = svc.create(OrderCreate.model_validate(base))
    again = svc.create(OrderCreate.model_validate(base))
    assert again.id == plain.id  # identical => idempotent

    with_services = svc.create(
        OrderCreate.model_validate({**base, "additionalServices": [_service_line()]})
    )
    # Same geometry + level, different services => different order.
    assert with_services.id != plain.id
    assert with_services.optimization_hash == plain.optimization_hash


# --- Public review: the client sees the services ------------------------------
def test_public_review_includes_services(client):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["additionalServices"] = [_service_line(unit_price=2.0, quantity=3)]
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]

    link = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    review = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]

    assert review["servicesTotal"] == 5.22
    assert review["total"] == 58.33
    assert len(review["additionalServices"]) == 1
    # The review projects the line as the client agreed it, tax included.
    assert review["additionalServices"][0]["lineTotal"] == 6.0


def test_list_additional_services_is_ordered_by_name(client):
    """Paging an unordered listing lets Postgres repeat or skip rows between pages."""
    _create_service(client, name="Perforación")
    _create_service(client, name="Biselado")
    _create_service(client, name="Lijado")

    listed = client.get("/api/v1/additional-services/").json()["data"]
    assert [s["name"] for s in listed] == ["Biselado", "Lijado", "Perforación"]

    recent = client.get(
        "/api/v1/additional-services/", params={"sort": "recent"}
    ).json()["data"]
    assert [s["name"] for s in recent] == ["Lijado", "Biselado", "Perforación"]
