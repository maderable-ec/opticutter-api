"""Tests for additional services (servicios adicionales).

Covers: the pure ``build_pricing`` fold (services added after the discount, not
discounted), the catalog CRUD + RBAC (admin writes, seller only reads), the
pre-order (quote) selection, the freeze into the order, and the public review
projection.
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
        "materials_summary": [
            {"material_key": "tablero", "product_id": 5, "total_cost": catalog_boards}
        ],
    }


def test_build_pricing_adds_services_after_discount():
    tier = {"code": "carpintero", "name": "Precio Carpintero", "rate": 0.02}
    services = [{"unit_price": 2.0, "quantity": 3}, {"unit_price": 15.0, "quantity": 1}]
    # The board is marked as discountable: services must land AFTER that discount.
    p = build_pricing(
        _payload(catalog_boards=100.0, edge=20.0), tier, services, {"tablero"}
    )
    assert p["subtotal"] == 120.0  # boards + edge at list price
    assert p["discount_amount"] == 2.0  # 2% only over the marked catalog board
    assert p["services_total"] == 21.0  # (2*3) + (15*1), not discounted
    assert p["total"] == 139.0  # 120 - 2 + 21


def test_build_pricing_without_services_is_unchanged():
    tier = {"code": "consumidor", "name": "Precio Consumidor", "rate": 0.0}
    p = build_pricing(_payload(catalog_boards=100.0, edge=20.0), tier)
    assert p["services_total"] == 0.0
    assert p["total"] == 120.0


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
    assert pricing["subtotal"] == 45.5
    assert pricing["servicesTotal"] == 6.0  # 2.0 * 3
    assert pricing["total"] == 51.5  # 45.5 + 6.0

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
    assert updated["optimization"]["pricing"]["servicesTotal"] == 10.0
    assert updated["optimization"]["pricing"]["total"] == 55.5  # 45.5 + 10


# --- Order: freeze ------------------------------------------------------------
def test_order_freezes_services(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["additionalServices"] = [_service_line(unit_price=2.0, quantity=3)]

    order = _mint_order(db_session, payload)
    data = client.get(f"/api/v1/orders/{order.id}").json()["data"]

    assert data["subtotal"] == 45.5
    assert data["additionalServicesTotal"] == 6.0
    assert data["total"] == 51.5
    assert len(data["additionalServices"]) == 1
    assert data["additionalServices"][0]["name"] == "Perforación"

    # Frozen against later edits: the column holds the total.
    db_session.expire_all()
    frozen = db_session.get(OrderModel, order.id)
    assert frozen.additional_services_total == 6.0
    assert frozen.total == 51.5


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
    # Same geometry + tier, different services => different order.
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

    assert review["servicesTotal"] == 6.0
    assert review["total"] == 51.5
    assert len(review["additionalServices"]) == 1
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
