"""Tests for the settings module: a single configuration, persisted and editable via API."""

from datetime import datetime

from src.shared.config import config


def _create_client(client):
    return client.post(
        "/api/v1/clients/",
        json={"identifier": "0100000397", "firstName": "Ada", "phone": "0100000397"},
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


def _optimize_payload(client_id, product_id):
    return {
        "clientId": client_id,
        "branchId": 1,  # default branch seeded by conftest
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 2,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }


# --- Cutting parameters --------------------------------------------------------
def test_get_cutting_seeds_from_config(client):
    """The first GET seeds the singleton row with ``config`` defaults."""
    resp = client.get("/api/v1/settings/cutting")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["kerf"] == config.KERF
    assert data["topTrim"] == config.TOP_TRIM
    assert data["edgeBandingWasteFactor"] == config.EDGE_BANDING_WASTE_FACTOR
    assert data["halfBoardMarkupPct"] == config.HALF_BOARD_MARKUP_PCT


def test_patch_cutting_persists_and_is_partial(client):
    """A partial PATCH persists only what was sent and doesn't overwrite the rest."""
    before = client.get("/api/v1/settings/cutting").json()["data"]

    resp = client.patch("/api/v1/settings/cutting", json={"kerf": 4.0})
    assert resp.status_code == 200
    assert resp.json()["data"]["kerf"] == 4.0

    after = client.get("/api/v1/settings/cutting").json()["data"]
    assert after["kerf"] == 4.0
    # The rest of the parameters stay intact.
    assert after["topTrim"] == before["topTrim"]
    assert after["rightTrim"] == before["rightTrim"]


def test_patch_cutting_rejects_negative(client):
    """Validation: cutting parameters cannot be negative (422)."""
    assert (
        client.patch("/api/v1/settings/cutting", json={"kerf": -1}).status_code == 422
    )
    assert (
        client.patch(
            "/api/v1/settings/cutting", json={"halfBoardMarkupPct": -1}
        ).status_code
        == 422
    )


def test_patch_cutting_half_board_markup_persists(client):
    """The half-board markup is partially patchable like the other cutting fields."""
    resp = client.patch("/api/v1/settings/cutting", json={"halfBoardMarkupPct": 0.20})
    assert resp.status_code == 200
    assert resp.json()["data"]["halfBoardMarkupPct"] == 0.20

    after = client.get("/api/v1/settings/cutting").json()["data"]
    assert after["halfBoardMarkupPct"] == 0.20


def test_cutting_settings_drive_optimization(client):
    """Changing the kerf in the DB changes the optimization hash (used by the optimizer)."""
    created_client = _create_client(client)
    board = _create_board(client)
    payload = _optimize_payload(created_client["id"], board["id"])

    first = client.post("/api/v1/optimize/", json=payload).json()["data"]

    client.patch("/api/v1/settings/cutting", json={"kerf": 99.0})
    second = client.post("/api/v1/optimize/", json=payload).json()["data"]

    assert first["optimizationHash"] != second["optimizationHash"]


def test_half_board_markup_drives_optimization_hash(client):
    """Changing the half-board markup changes the optimization hash too."""
    created_client = _create_client(client)
    board = _create_board(client)
    payload = _optimize_payload(created_client["id"], board["id"])

    first = client.post("/api/v1/optimize/", json=payload).json()["data"]

    client.patch("/api/v1/settings/cutting", json={"halfBoardMarkupPct": 0.30})
    second = client.post("/api/v1/optimize/", json=payload).json()["data"]

    assert first["optimizationHash"] != second["optimizationHash"]


# --- Pre-orders (mutable quote) -------------------------------------------------
def test_get_preorders_seeds_from_config(client):
    """The first GET seeds the preorders section with ``config`` defaults."""
    resp = client.get("/api/v1/settings/preorders")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["preorderValidityDays"] == config.PREORDER_VALIDITY_DAYS
    assert data["maxOpenPreordersPerClient"] == config.MAX_OPEN_PREORDERS_PER_CLIENT


def test_patch_preorders_persists_and_is_partial(client):
    """A partial PATCH persists only what was sent and doesn't overwrite the rest."""
    before = client.get("/api/v1/settings/preorders").json()["data"]

    resp = client.patch("/api/v1/settings/preorders", json={"preorderValidityDays": 30})
    assert resp.status_code == 200
    assert resp.json()["data"]["preorderValidityDays"] == 30

    after = client.get("/api/v1/settings/preorders").json()["data"]
    assert after["preorderValidityDays"] == 30
    # The cap stays intact.
    assert after["maxOpenPreordersPerClient"] == before["maxOpenPreordersPerClient"]


def test_patch_preorders_rejects_below_one(client):
    """Validation: validity and cap must be ≥ 1 (422)."""
    assert (
        client.patch(
            "/api/v1/settings/preorders", json={"preorderValidityDays": 0}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/v1/settings/preorders", json={"maxOpenPreordersPerClient": 0}
        ).status_code
        == 422
    )


def test_preorder_validity_comes_from_settings(client):
    """The pre-order takes its validity from settings (not env): the gap = config days."""
    created_client = _create_client(client)
    board = _create_board(client)

    client.patch("/api/v1/settings/preorders", json={"preorderValidityDays": 7})
    pre = client.post(
        "/api/v1/preorders/", json=_optimize_payload(created_client["id"], board["id"])
    ).json()["data"]

    created = datetime.fromisoformat(pre["createdAt"])
    expires = datetime.fromisoformat(pre["expiresAt"])
    assert (expires - created).days == 7


# --- Company data -----------------------------------------------------------
def test_get_company_seeds_from_config(client):
    """The first GET exposes the company data seeded from ``config``."""
    resp = client.get("/api/v1/settings/company")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == config.COMPANY_NAME
    assert data["phone"] == config.COMPANY_PHONE
    assert isinstance(data["branches"], list)


def test_patch_company_persists(client):
    """The company PATCH persists phone and branches (list of objects)."""
    resp = client.patch(
        "/api/v1/settings/company",
        json={
            "phone": "0100000017",
            "branches": [{"name": "Matriz", "address": "Av. Siempre Viva 123"}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["phone"] == "0100000017"
    assert data["branches"] == [{"name": "Matriz", "address": "Av. Siempre Viva 123"}]

    after = client.get("/api/v1/settings/company").json()["data"]
    assert after["phone"] == "0100000017"
    assert after["branches"][0]["name"] == "Matriz"


def test_company_settings_render_in_proforma(client):
    """The quote proforma uses the current company data (live letterhead).

    The optimizer no longer emits a document: the proforma lives in the quote
    (live prices) and in the order. Here it's exercised via the pre-order.
    """
    created_client = _create_client(client)
    board = _create_board(client)
    preorder = client.post(
        "/api/v1/preorders/", json=_optimize_payload(created_client["id"], board["id"])
    ).json()["data"]

    client.patch("/api/v1/settings/company", json={"phone": "0999999999"})

    proforma = client.get(f"/api/v1/preorders/{preorder['id']}/proforma")
    assert proforma.status_code == 200
    assert proforma.headers["content-type"] == "application/pdf"
    assert len(proforma.content) > 1000
