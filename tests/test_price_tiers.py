"""Tests for the price tier system (discount by client type).

Covers: the pure ``build_pricing`` transform, the settings config (GET/PATCH), the
selection in the pre-order (quote), the freeze + dedupe in the order, and the
public review projection.
"""

from src.modules.optimizations.pricing import build_pricing
from src.modules.orders.model import OrderModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService

from .test_orders import _create_board, _create_client, _order_payload


# --- build_pricing (pure layer) -----------------------------------------------
def _payload(*, catalog_boards=100.0, offcut_boards=0.0, edge=0.0):
    """Minimal payload with one catalog board and (optionally) one off-catalog board.

    The catalog board is keyed ``tablero``; the off-catalog one ``retazo``. Both
    keys can be handed to ``build_pricing`` so the tests can state which boards
    the seller marked.
    """
    materials_summary = [
        {"material_key": "tablero", "product_id": 5, "total_cost": catalog_boards},
    ]
    if offcut_boards:
        materials_summary.append(
            {"material_key": "retazo", "product_id": None, "total_cost": offcut_boards}
        )
    return {
        "total_boards_cost": round(catalog_boards + offcut_boards, 2),
        "total_edge_banding_cost": edge,
        "materials_summary": materials_summary,
    }


def test_build_pricing_consumidor_is_no_discount():
    tier = {"code": "consumidor", "name": "Precio Consumidor", "rate": 0.0}
    p = build_pricing(
        _payload(catalog_boards=100.0, edge=20.0), tier, None, {"tablero"}
    )
    assert p["subtotal"] == 120.0
    assert p["discount_amount"] == 0.0
    assert p["total"] == 120.0
    assert p["price_tier_code"] == "consumidor"


def test_build_pricing_without_a_selection_discounts_nothing():
    # The default: a tier with a rate but no board marked bills at list price.
    # This is what makes the checkbox opt-in rather than opt-out.
    tier = {"code": "carpintero", "name": "Precio Carpintero", "rate": 0.02}
    p = build_pricing(_payload(catalog_boards=100.0, edge=20.0), tier)
    assert p["discount_base"] == 0.0
    assert p["discount_amount"] == 0.0
    assert p["total"] == p["subtotal"] == 120.0


def test_build_pricing_discounts_only_the_selected_boards():
    # Two catalog boards, only the first one marked: the base is that one alone.
    tier = {"code": "carpintero", "name": "Precio Carpintero", "rate": 0.02}
    payload = {
        "total_boards_cost": 150.0,
        "total_edge_banding_cost": 20.0,
        "materials_summary": [
            {"material_key": "melamina", "product_id": 5, "total_cost": 100.0},
            {"material_key": "mdf", "product_id": 7, "total_cost": 50.0},
        ],
    }
    p = build_pricing(payload, tier, None, {"melamina"})
    assert p["discount_base"] == 100.0
    assert p["subtotal"] == 170.0  # everything at list price
    assert p["discount_amount"] == 2.0
    assert p["total"] == 168.0


def test_build_pricing_never_discounts_off_catalog_materials():
    # 100 catalog + 50 offcut (product_id None) + 20 edge banding, BOTH keys marked.
    # The offcut still can't be discounted: the selection narrows the catalog
    # boards, it doesn't widen the base beyond them.
    tier = {"code": "carpintero", "name": "Precio Carpintero", "rate": 0.02}
    p = build_pricing(
        _payload(catalog_boards=100.0, offcut_boards=50.0, edge=20.0),
        tier,
        None,
        {"tablero", "retazo"},
    )
    assert p["discount_base"] == 100.0
    assert p["subtotal"] == 170.0
    assert p["discount_amount"] == 2.0
    assert p["total"] == 168.0


def test_build_pricing_half_board_line_follows_its_material():
    # A material billed as one full board + one half board yields TWO summary
    # lines sharing a material_key, so one checkbox covers both.
    tier = {"code": "efectivo", "name": "Precio Efectivo", "rate": 0.05}
    payload = {
        "total_boards_cost": 90.0,
        "total_edge_banding_cost": 0.0,
        "materials_summary": [
            {"material_key": "melamina", "product_id": 5, "total_cost": 60.0},
            {
                "material_key": "melamina",
                "product_id": 5,
                "total_cost": 30.0,
                "half_board": True,
            },
        ],
    }
    p = build_pricing(payload, tier, None, {"melamina"})
    assert p["discount_base"] == 90.0
    assert p["discount_amount"] == 4.5


def test_build_pricing_efectivo_rounds_to_cents():
    tier = {"code": "efectivo", "name": "Precio Efectivo", "rate": 0.05}
    p = build_pricing(_payload(catalog_boards=45.5), tier, None, {"tablero"})
    assert p["discount_amount"] == round(45.5 * 0.05, 2)
    assert p["total"] == round(45.5 - p["discount_amount"], 2)


# --- Tier config in settings -------------------------------------------
def test_get_price_tiers_lists_active_sorted(client):
    resp = client.get("/api/v1/settings/price-tiers")
    assert resp.status_code == 200
    tiers = resp.json()["data"]
    assert [t["code"] for t in tiers] == ["consumidor", "carpintero", "efectivo"]
    assert tiers[1]["rate"] == 0.02
    assert all(t["isActive"] for t in tiers)


def test_patch_price_tiers_changes_rate(client):
    new_tiers = [
        {
            "code": "consumidor",
            "name": "Precio Consumidor",
            "rate": 0.0,
            "isActive": True,
            "sortOrder": 1,
        },
        {
            "code": "carpintero",
            "name": "Precio Carpintero",
            "rate": 0.03,
            "isActive": True,
            "sortOrder": 2,
        },
    ]
    resp = client.patch("/api/v1/settings/price-tiers", json={"priceTiers": new_tiers})
    assert resp.status_code == 200
    after = client.get("/api/v1/settings/price-tiers").json()["data"]
    assert {t["code"]: t["rate"] for t in after} == {
        "consumidor": 0.0,
        "carpintero": 0.03,
    }


# --- Pre-order (quote): live selection ----------------------------------
def _discounting(payload):
    """Marks the payload's single board as discountable (the seller's checkbox)."""
    payload["materials"][0]["applyDiscount"] = True
    return payload


def test_preorder_create_applies_discount_to_optimization(client):
    c = _create_client(client)
    b = _create_board(client)  # price 45.5, 1 board used
    payload = _discounting(_order_payload(c["id"], b["id"]))
    payload["priceTierCode"] = "carpintero"

    resp = client.post("/api/v1/preorders/", json=payload)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["priceTierCode"] == "carpintero"

    pricing = data["optimization"]["pricing"]
    assert pricing["priceTierCode"] == "carpintero"
    assert pricing["discountRate"] == 0.02
    assert pricing["subtotal"] == 45.5  # at list price
    assert pricing["discountAmount"] == 0.91
    assert pricing["total"] == 44.59


def test_preorder_without_marked_boards_is_billed_at_list_price(client):
    # The tier alone no longer discounts anything: a board has to be marked.
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["priceTierCode"] = "carpintero"

    data = client.post("/api/v1/preorders/", json=payload).json()["data"]
    pricing = data["optimization"]["pricing"]
    assert pricing["discountRate"] == 0.02  # the tier is still the selected one
    assert pricing["discountBase"] == 0.0
    assert pricing["discountAmount"] == 0.0
    assert pricing["total"] == pricing["subtotal"] == 45.5


def test_preorder_discount_selection_survives_an_edit(client):
    # The flag rides inside `materials`, so a PUT that resends them moves it.
    c = _create_client(client)
    b = _create_board(client)
    payload = _discounting(_order_payload(c["id"], b["id"]))
    payload["priceTierCode"] = "carpintero"
    created = client.post("/api/v1/preorders/", json=payload).json()["data"]
    assert created["optimization"]["pricing"]["discountAmount"] == 0.91

    # Unmark the board: same cuts, same tier, no discount.
    unmarked = [{**payload["materials"][0], "applyDiscount": False}]
    resp = client.put(
        f"/api/v1/preorders/{created['id']}",
        json={"materials": unmarked, "requirements": payload["requirements"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["optimization"]["pricing"]["discountAmount"] == 0.0


def test_preorder_create_rejects_unknown_tier(client):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["priceTierCode"] = "mayorista"

    resp = client.post("/api/v1/preorders/", json=payload)
    assert resp.status_code == 422
    assert "Nivel de precio" in resp.json()["errors"][0]["message"]


# --- Order: freeze + audit + dedupe -----------------------------------
def test_order_freezes_discount(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _discounting(_order_payload(c["id"], b["id"]))
    payload["priceTierCode"] = "carpintero"

    order = OrderService(db_session).create(OrderCreate.model_validate(payload))
    data = client.get(f"/api/v1/orders/{order.id}").json()["data"]

    assert data["priceTierCode"] == "carpintero"
    assert data["discountRate"] == 0.02
    assert data["subtotal"] == 45.5
    assert data["discountAmount"] == 0.91
    assert data["total"] == 44.59  # subtotal != total
    # Lines stay at list price (discount applies only at document level).
    assert data["lines"][0]["unitPriceSnapshot"] == 45.5


def test_order_discount_rate_frozen_against_settings_change(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _discounting(_order_payload(c["id"], b["id"]))
    payload["priceTierCode"] = "carpintero"
    order = OrderService(db_session).create(OrderCreate.model_validate(payload))

    # Changing the carpintero rate to 10% must NOT alter the already-created order.
    client.patch(
        "/api/v1/settings/price-tiers",
        json={
            "priceTiers": [
                {
                    "code": "carpintero",
                    "name": "Precio Carpintero",
                    "rate": 0.10,
                    "isActive": True,
                    "sortOrder": 1,
                },
            ]
        },
    )
    db_session.expire_all()
    frozen = db_session.get(OrderModel, order.id)
    assert frozen.discount_rate == 0.02
    assert frozen.total == 44.59


def test_dedupe_distinguishes_price_tiers(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    svc = OrderService(db_session)

    base = _order_payload(c["id"], b["id"])
    consumidor = svc.create(
        OrderCreate.model_validate({**base, "priceTierCode": "consumidor"})
    )
    # Same geometry, same tier => idempotent (same order).
    again = svc.create(
        OrderCreate.model_validate({**base, "priceTierCode": "consumidor"})
    )
    assert again.id == consumidor.id
    # Same geometry, different tier => different order (tier isn't part of the hash).
    carpintero = svc.create(
        OrderCreate.model_validate({**base, "priceTierCode": "carpintero"})
    )
    assert carpintero.id != consumidor.id
    assert carpintero.optimization_hash == consumidor.optimization_hash


def test_dedupe_distinguishes_discounted_board_selections(client, db_session):
    # The per-board selection isn't in the hash either, so it has to be part of
    # the dedupe key: otherwise a re-quote with a board unmarked would silently
    # return the discounted order.
    c = _create_client(client)
    b = _create_board(client)
    svc = OrderService(db_session)

    base = _order_payload(c["id"], b["id"])
    base["priceTierCode"] = "carpintero"
    plain = svc.create(OrderCreate.model_validate(base))

    marked = _discounting(_order_payload(c["id"], b["id"]))
    marked["priceTierCode"] = "carpintero"
    discounted = svc.create(OrderCreate.model_validate(marked))
    assert discounted.id != plain.id
    assert discounted.optimization_hash == plain.optimization_hash
    assert plain.discount_amount == 0.0
    assert discounted.discount_amount == 0.91


def test_proforma_prints_one_discount_line_for_the_selection(client):
    """The document keeps a single ``Descuento`` row — it just adds up less.

    The per-board choice is the seller's, so the PDF must not expose which board
    got the better price: every line prints at list price and the totals block
    shows one adjustment, over the marked board only.
    """
    import io

    from pypdf import PdfReader

    c = _create_client(client)
    marked = _create_board(client, code="MEL18")  # 45.5, discounted
    plain = _create_board(client, code="MDF15")  # 45.5, NOT discounted

    payload = _order_payload(c["id"], marked["id"])
    payload["priceTierCode"] = "carpintero"
    payload["materials"][0]["applyDiscount"] = True
    payload["materials"].append(
        {"key": "b2", "source": "catalog", "productId": plain["id"]}
    )
    payload["requirements"].append({**payload["requirements"][0], "materialKey": "b2"})

    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]
    pricing = pre["optimization"]["pricing"]
    # Both boards are billed; only the marked one is the discount base.
    assert pricing["subtotal"] == 91.0
    assert pricing["discountBase"] == 45.5
    assert pricing["discountAmount"] == 0.91

    pdf = client.get(f"/api/v1/preorders/{pre['id']}/proforma")
    assert pdf.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
    )
    assert "Descuento Precio Carpintero (-2%):" in text
    assert "-$0.91" in text
    # Both boards still print at list price; nothing marks the discounted one.
    assert text.count("$45.50") >= 2


# --- Public review: the client sees the discount ----------------------------
def test_public_review_reflects_discount(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _discounting(_order_payload(c["id"], b["id"]))
    payload["priceTierCode"] = "carpintero"
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]

    link = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    review = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]

    assert review["subtotal"] == 45.5
    assert review["discountRate"] == 0.02
    assert review["discountAmount"] == 0.91
    assert review["total"] == 44.59
    assert review["priceTierName"] == "Precio Carpintero"
