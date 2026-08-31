"""Tests for the catalog price levels and the explicit sales tax.

Covers: the pure ``build_pricing`` transform, the tax config (GET/PATCH), the
level selection in the pre-order (quote), the freeze + dedupe in the order, and
the public review projection. The re-pricing pass itself
(``apply_price_level``) is unit-tested in ``tests/unit/test_price_levels.py``.
"""

from src.modules.optimizations.pricing import build_pricing
from src.modules.orders.model import OrderModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService

from .test_orders import _create_board, _create_client, _order_payload


# --- build_pricing (pure layer) -----------------------------------------------
def _payload(*, boards=100.0, edge=0.0):
    """Minimal already-leveled payload: the boards cost what they cost."""
    return {"total_boards_cost": boards, "total_edge_banding_cost": edge}


def test_build_pricing_adds_the_tax_over_everything():
    p = build_pricing(_payload(boards=100.0, edge=20.0), 1, None, 0.15)
    assert p["subtotal"] == 120.0
    assert p["tax_amount"] == 18.0
    assert p["total"] == 138.0
    assert p["price_level"] == 1
    assert p["price_level_name"] == "Precio 1"


def test_build_pricing_names_the_level():
    p = build_pricing(_payload(), 3, None, 0.15)
    assert p["price_level"] == 3
    assert p["price_level_name"] == "Precio 3"


def test_build_pricing_converts_services_to_net():
    """Services are registered tax-included; the subtotal is net, so they convert.

    $40.00 with 15% inside is $34.78 net; the tax line then puts the 15% back, so
    the client still pays the $40 the seller quoted for the service.
    """
    services = [{"name": "Perforación", "unit_price": 40.0, "quantity": 1}]
    p = build_pricing(_payload(boards=0.0), 1, services, 0.15)
    assert p["services_total"] == 34.78
    assert p["subtotal"] == 34.78
    assert p["tax_amount"] == 5.22
    assert p["total"] == 40.0


def test_build_pricing_with_no_tax_leaves_the_subtotal_alone():
    p = build_pricing(_payload(boards=45.5), 1, None, 0.0)
    assert p["tax_amount"] == 0.0
    assert p["total"] == p["subtotal"] == 45.5


# --- Tax config in settings ----------------------------------------------------
def test_get_tax_settings_returns_the_seeded_rate(client):
    resp = client.get("/api/v1/settings/taxes")
    assert resp.status_code == 200
    assert resp.json()["data"]["taxRate"] == 0.15


def test_patch_tax_rate_changes_what_new_quotes_bill(client):
    c = _create_client(client)
    b = _create_board(client)  # net 45.5, 1 board used

    assert (
        client.patch("/api/v1/settings/taxes", json={"taxRate": 0.12}).status_code
        == 200
    )
    assert client.get("/api/v1/settings/taxes").json()["data"]["taxRate"] == 0.12

    pre = client.post(
        "/api/v1/preorders/", json=_order_payload(c["id"], b["id"])
    ).json()["data"]
    pricing = pre["optimization"]["pricing"]
    assert pricing["taxRate"] == 0.12
    assert pricing["taxAmount"] == 5.46
    assert pricing["total"] == 50.96


def test_patch_tax_rate_rejects_a_ratio_above_one(client):
    assert (
        client.patch("/api/v1/settings/taxes", json={"taxRate": 15}).status_code == 422
    )


# --- Pre-order (quote): live level selection -----------------------------------
def _leveled(payload):
    """Marks the payload's single board for the quote's price level."""
    payload["materials"][0]["applyPriceLevel"] = True
    return payload


def _create_leveled_board(client, code="MEL18", price=45.5, price_2=40.0, price_3=None):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": f"Melamina {code}",
            "price": price,
            "price2": price_2,
            "price3": price_3,
            "attributes": {"height": 2440, "width": 1220, "thickness": 18},
        },
    ).json()["data"]


def test_preorder_bills_the_marked_board_at_the_chosen_level(client):
    c = _create_client(client)
    b = _create_leveled_board(client)
    payload = _leveled(_order_payload(c["id"], b["id"]))
    payload["priceLevel"] = 2

    resp = client.post("/api/v1/preorders/", json=payload)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["priceLevel"] == 2

    pricing = data["optimization"]["pricing"]
    assert pricing["priceLevel"] == 2
    assert pricing["subtotal"] == 40.0  # the level's price, not 45.5
    assert pricing["taxAmount"] == 6.0
    assert pricing["total"] == 46.0
    # The line itself carries the level's price: there is no discount row.
    assert data["optimization"]["materialsSummary"][0]["costPerUnit"] == 40.0


def test_preorder_without_marked_boards_is_billed_at_list_price(client):
    # The level alone changes nothing: a board has to be marked. This is what
    # makes the checkbox opt-in rather than opt-out.
    c = _create_client(client)
    b = _create_leveled_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["priceLevel"] = 2

    pricing = client.post("/api/v1/preorders/", json=payload).json()["data"][
        "optimization"
    ]["pricing"]
    assert pricing["priceLevel"] == 2  # still the selected one
    assert pricing["subtotal"] == 45.5


def test_a_level_the_catalog_never_loaded_falls_back_to_the_list_price(client):
    """``price3`` is null here — billing must not read that as free."""
    c = _create_client(client)
    b = _create_leveled_board(client, price_3=None)
    payload = _leveled(_order_payload(c["id"], b["id"]))
    payload["priceLevel"] = 3

    pricing = client.post("/api/v1/preorders/", json=payload).json()["data"][
        "optimization"
    ]["pricing"]
    assert pricing["subtotal"] == 45.5


def test_preorder_level_selection_survives_an_edit(client):
    # The flag rides inside `materials`, so a PUT that resends them moves it.
    c = _create_client(client)
    b = _create_leveled_board(client)
    payload = _leveled(_order_payload(c["id"], b["id"]))
    payload["priceLevel"] = 2
    created = client.post("/api/v1/preorders/", json=payload).json()["data"]
    assert created["optimization"]["pricing"]["subtotal"] == 40.0

    unmarked = [{**payload["materials"][0], "applyPriceLevel": False}]
    resp = client.put(
        f"/api/v1/preorders/{created['id']}",
        json={"materials": unmarked, "requirements": payload["requirements"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["optimization"]["pricing"]["subtotal"] == 45.5


def test_preorder_rejects_a_level_outside_the_catalog(client):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["priceLevel"] = 4
    assert client.post("/api/v1/preorders/", json=payload).status_code == 422


# --- Order: freeze + audit + dedupe --------------------------------------------
def test_order_freezes_the_level_and_the_tax(client, db_session):
    c = _create_client(client)
    b = _create_leveled_board(client)
    payload = _leveled(_order_payload(c["id"], b["id"]))
    payload["priceLevel"] = 2

    order = OrderService(db_session).create(OrderCreate.model_validate(payload))
    data = client.get(f"/api/v1/orders/{order.id}").json()["data"]

    assert data["priceLevel"] == 2
    assert data["subtotal"] == 40.0
    assert data["taxRate"] == 0.15
    assert data["taxAmount"] == 6.0
    assert data["total"] == 46.0
    # The frozen line carries the level's price, which is what the PDF prints.
    assert data["lines"][0]["unitPriceSnapshot"] == 40.0


def test_order_tax_rate_frozen_against_settings_change(client, db_session):
    """Raising the tax rate must never rewrite a document already issued."""
    c = _create_client(client)
    b = _create_board(client)
    order = OrderService(db_session).create(
        OrderCreate.model_validate(_order_payload(c["id"], b["id"]))
    )
    assert order.tax_rate == 0.15

    client.patch("/api/v1/settings/taxes", json={"taxRate": 0.12})
    db_session.expire_all()
    frozen = db_session.get(OrderModel, order.id)
    assert frozen.tax_rate == 0.15
    assert frozen.tax_amount == 6.83
    assert frozen.total == 52.33


def test_dedupe_distinguishes_price_levels(client, db_session):
    c = _create_client(client)
    b = _create_leveled_board(client)
    svc = OrderService(db_session)

    base = _leveled(_order_payload(c["id"], b["id"]))
    level_1 = svc.create(OrderCreate.model_validate({**base, "priceLevel": 1}))
    # Same geometry, same level => idempotent (same order).
    again = svc.create(OrderCreate.model_validate({**base, "priceLevel": 1}))
    assert again.id == level_1.id
    # Same geometry, different level => different order (it isn't in the hash).
    level_2 = svc.create(OrderCreate.model_validate({**base, "priceLevel": 2}))
    assert level_2.id != level_1.id
    assert level_2.optimization_hash == level_1.optimization_hash
    assert level_2.subtotal == 40.0


def test_dedupe_distinguishes_marked_board_selections(client, db_session):
    # The per-board selection isn't in the hash either, so a re-quote with the
    # board unmarked must not silently return the cheaper order.
    c = _create_client(client)
    b = _create_leveled_board(client)
    svc = OrderService(db_session)

    plain = svc.create(
        OrderCreate.model_validate(
            {**_order_payload(c["id"], b["id"]), "priceLevel": 2}
        )
    )
    marked = svc.create(
        OrderCreate.model_validate(
            {**_leveled(_order_payload(c["id"], b["id"])), "priceLevel": 2}
        )
    )
    assert marked.id != plain.id
    assert marked.optimization_hash == plain.optimization_hash
    assert plain.subtotal == 45.5
    assert marked.subtotal == 40.0


def test_proforma_prints_the_level_price_and_one_tax_line(client):
    """No discount row: each line already shows what it costs.

    The per-board choice is the seller's, so the PDF must not announce which
    board got the better price — it just prints the price.
    """
    import io

    from pypdf import PdfReader

    c = _create_client(client)
    marked = _create_leveled_board(client, code="MEL18")  # 45.50 -> 40.00
    plain = _create_leveled_board(client, code="MDF15")  # billed at list

    payload = _leveled(_order_payload(c["id"], marked["id"]))
    payload["priceLevel"] = 2
    payload["materials"].append(
        {"key": "b2", "source": "catalog", "productId": plain["id"]}
    )
    payload["requirements"].append({**payload["requirements"][0], "materialKey": "b2"})

    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]
    pricing = pre["optimization"]["pricing"]
    assert pricing["subtotal"] == 85.5  # 40.00 marked + 45.50 at list
    # 85.5 * 0.15 = 12.825, and Python rounds that half to even -> 12.82. The
    # server is deliberately the only place money is rounded (JS would say 12.83).
    assert pricing["taxAmount"] == 12.82
    assert pricing["total"] == 98.32

    pdf = client.get(f"/api/v1/preorders/{pre['id']}/proforma")
    assert pdf.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
    )
    assert "IVA (15%):" in text
    assert "$12.82" in text
    assert "Descuento" not in text
    assert "$40.00" in text and "$45.50" in text


# --- Public review: the client sees the tax ------------------------------------
def test_public_review_reflects_the_level_and_the_tax(client, db_session):
    c = _create_client(client)
    b = _create_leveled_board(client)
    payload = _leveled(_order_payload(c["id"], b["id"]))
    payload["priceLevel"] = 2
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]

    link = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    review = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]

    assert review["subtotal"] == 40.0
    assert review["taxRate"] == 0.15
    assert review["taxAmount"] == 6.0
    assert review["total"] == 46.0
    assert review["priceLevelName"] == "Precio 2"
