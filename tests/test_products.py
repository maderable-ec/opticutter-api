"""Tests for the products module (multi-type catalog: CRUD + per-type validation)."""


def _board_payload(code="MEL18", name="Melamina 18mm"):
    return {
        "type": "board",
        "code": code,
        "name": name,
        "description": "Tablero estándar",
        "price": 45.5,
        "attributes": {
            "height": 2440,
            "width": 1220,
            "thickness": 18,
            "grainDirection": "v",
        },
    }


def _edge_banding_payload(code="TAP22", name="Tapacanto PVC 22mm"):
    return {
        "type": "edge_banding",
        "code": code,
        "name": name,
        "price": 0.8,
        "attributes": {"length": 50000, "width": 22, "thickness": 1, "color": "blanco"},
    }


def test_create_and_get_board_product(client):
    resp = client.post("/api/v1/products/", json=_board_payload())
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["type"] == "board"
    assert created["code"] == "MEL18"
    assert created["price"] == 45.5
    # Attributes are persisted/returned in their canonical camelCase form.
    assert created["attributes"]["height"] == 2440
    assert created["attributes"]["grainDirection"] == "v"

    got = client.get(f"/api/v1/products/{created['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["name"] == "Melamina 18mm"


def test_create_edge_banding_product(client):
    resp = client.post("/api/v1/products/", json=_edge_banding_payload())
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["type"] == "edge_banding"
    assert created["attributes"]["length"] == 50000
    assert created["attributes"]["color"] == "blanco"


def test_board_missing_required_attribute_returns_422(client):
    """The discriminator validates ``attributes`` per type (board without height)."""
    payload = _board_payload()
    del payload["attributes"]["height"]
    resp = client.post("/api/v1/products/", json=payload)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert errors[0]["code"] == "VALIDATION_ERROR"
    assert any(e["field"] and e["field"].startswith("body.") for e in errors)


def test_unknown_product_type_returns_422(client):
    payload = _board_payload()
    payload["type"] = "hammer"
    assert client.post("/api/v1/products/", json=payload).status_code == 422


def test_create_duplicate_code_returns_409(client):
    client.post("/api/v1/products/", json=_board_payload())
    dup = client.post("/api/v1/products/", json=_board_payload(name="Otro nombre"))
    assert dup.status_code == 409
    error = dup.json()["errors"][0]
    assert error["code"] == "CONFLICT"
    assert error["message"] == "El código del producto ya existe"


def test_create_duplicate_name_returns_409(client):
    client.post("/api/v1/products/", json=_board_payload())
    dup = client.post("/api/v1/products/", json=_board_payload(code="MEL15"))
    assert dup.status_code == 409
    assert dup.json()["errors"][0]["message"] == "El nombre del producto ya existe"


def test_get_missing_product_returns_404(client):
    assert client.get("/api/v1/products/999999").status_code == 404


def test_list_search_and_filter_by_type(client):
    client.post("/api/v1/products/", json=_board_payload(code="MEL18", name="Blanco"))
    client.post("/api/v1/products/", json=_board_payload(code="MDF15", name="Roble"))
    client.post("/api/v1/products/", json=_edge_banding_payload())

    listed = client.get("/api/v1/products/")
    body = listed.json()
    assert body["meta"]["pagination"]["total"] == 3

    boards = client.get("/api/v1/products/", params={"type": "board"}).json()
    assert boards["meta"]["pagination"]["total"] == 2
    assert all(p["type"] == "board" for p in boards["data"])

    found = client.get(
        "/api/v1/products/", params={"type": "board", "search": "Roble"}
    ).json()
    assert [p["code"] for p in found["data"]] == ["MDF15"]
    assert found["meta"]["pagination"]["total"] == 1


def test_list_is_ordered_by_name_and_pages_do_not_overlap(client):
    """Paging is only safe with a total order: without it Postgres may repeat/skip rows."""
    for code, name in (("C", "Cedro"), ("A", "Abeto"), ("B", "Bambu")):
        client.post("/api/v1/products/", json=_board_payload(code=code, name=name))

    listed = client.get("/api/v1/products/").json()["data"]
    assert [p["name"] for p in listed] == ["Abeto", "Bambu", "Cedro"]

    seen = []
    for offset in range(3):
        page = client.get(
            "/api/v1/products/", params={"limit": 1, "offset": offset}
        ).json()
        assert page["meta"]["pagination"]["total"] == 3
        seen.extend(p["name"] for p in page["data"])
    assert seen == ["Abeto", "Bambu", "Cedro"]


def test_list_filter_by_is_active(client):
    client.post("/api/v1/products/", json=_board_payload(code="ON", name="Activo"))
    off = _board_payload(code="OFF", name="Inactivo")
    off["isActive"] = False
    client.post("/api/v1/products/", json=off)

    # Omitting the filter keeps listing both: the catalog admin manages inactive products.
    assert client.get("/api/v1/products/").json()["meta"]["pagination"]["total"] == 2

    active = client.get("/api/v1/products/", params={"is_active": True}).json()
    assert [p["code"] for p in active["data"]] == ["ON"]
    inactive = client.get("/api/v1/products/", params={"is_active": False}).json()
    assert [p["code"] for p in inactive["data"]] == ["OFF"]


def test_get_product_by_code(client):
    client.post("/api/v1/products/", json=_board_payload(code="ABC123"))
    ok = client.get("/api/v1/products/code/ABC123")
    assert ok.status_code == 200
    assert ok.json()["data"]["code"] == "ABC123"
    assert client.get("/api/v1/products/code/NOPE").status_code == 404


def test_update_common_fields_and_attributes(client):
    created = client.post("/api/v1/products/", json=_board_payload()).json()["data"]

    upd = client.put(
        f"/api/v1/products/{created['id']}",
        json={
            "price": 60.0,
            "attributes": {"height": 2500, "width": 1220, "thickness": 18},
        },
    )
    assert upd.status_code == 200
    data = upd.json()["data"]
    assert data["price"] == 60.0
    assert data["attributes"]["height"] == 2500


def test_delete_product(client):
    created = client.post("/api/v1/products/", json=_board_payload()).json()["data"]
    assert client.delete(f"/api/v1/products/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/products/{created['id']}").status_code == 404


# --- Board -> coordinated edge-banding matching --------------------------------


def _seed_board(client, code, name, thickness, family="CASHMERE"):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": name,
            "price": 50.0,
            "attributes": {
                "height": 2800,
                "width": 2070,
                "thickness": thickness,
                "family": family,
            },
        },
    ).json()["data"]


def _seed_edge(
    client, code, name, band_type, thickness, width, color, family="CASHMERE"
):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": code,
            "name": name,
            "price": 12.0,
            "attributes": {
                "bandType": band_type,
                "thickness": thickness,
                "width": width,
                "color": color,
                "family": family,
            },
        },
    ).json()["data"]


def _seed_cashmere_catalog(client):
    """Cashmere board 15 and 36 + their coordinated edge bandings (real-seed style)."""
    _seed_board(client, "MDP-SL-CSH-15", "MDP 15mm Cashmere", 15)
    _seed_board(client, "MDP-SL-CSH-36", "MDP 36mm Cashmere", 36)
    _seed_edge(
        client,
        "TAP-SL-CSH-045",
        "Tapacanto Cashmere Suave 0.45x19mm",
        "Soft",
        0.45,
        19,
        "Cashmere",
    )
    _seed_edge(
        client,
        "TAP-SL-CSH-100",
        "Tapacanto Cashmere Duro 1x40mm",
        "Hard",
        1.0,
        40,
        "Cashmere",
    )
    _seed_edge(
        client,
        "TAP-SL-CSH-150",
        "Tapacanto Cashmere Duro 1.5x19mm",
        "Hard",
        1.5,
        19,
        "Cashmere",
    )


def test_edge_bandings_for_15mm_board(client):
    _seed_cashmere_catalog(client)
    board = client.get("/api/v1/products/code/MDP-SL-CSH-15").json()["data"]

    resp = client.get(f"/api/v1/products/{board['id']}/edge-bandings")
    assert resp.status_code == 200
    bands = resp.json()["data"]
    # 15mm -> width 19: Soft 0.45 and Hard 1.5 (sorted by thickness)
    assert [b["attributes"]["width"] for b in bands] == [19, 19]
    assert [b["attributes"]["bandType"] for b in bands] == ["Soft", "Hard"]

    # The BandType enum accepts case-insensitive input ("soft") and the Spanish
    # alias ("suave"), both normalized to the canonical English value.
    for value in ("soft", "suave"):
        soft = client.get(
            f"/api/v1/products/{board['id']}/edge-bandings",
            params={"band_type": value},
        ).json()["data"]
        assert len(soft) == 1
        assert soft[0]["code"] == "TAP-SL-CSH-045"


def test_edge_bandings_for_36mm_board_only_hard(client):
    _seed_cashmere_catalog(client)
    board = client.get("/api/v1/products/code/MDP-SL-CSH-36").json()["data"]

    bands = client.get(f"/api/v1/products/{board['id']}/edge-bandings").json()["data"]
    # 36mm -> width 40: only the Hard 1.0x40 exists
    assert len(bands) == 1
    assert bands[0]["code"] == "TAP-SL-CSH-100"
    assert bands[0]["attributes"]["width"] == 40

    # No Soft banding for 36mm: real catalog gap -> empty list
    soft = client.get(
        f"/api/v1/products/{board['id']}/edge-bandings", params={"band_type": "Soft"}
    ).json()["data"]
    assert soft == []


def test_edge_bandings_excludes_other_designs(client):
    """Must not return edge bandings from a different family even if names/codes share tokens."""
    _seed_cashmere_catalog(client)
    # Different family: the board's family has no coordinated edge banding seeded, and a
    # banding of yet another family must not leak in.
    _seed_board(
        client, "MDP-RO-BRD-15", "MDP 15mm Barroco Dorado", 15, family="BARROCO_DORADO"
    )
    _seed_edge(
        client,
        "TAP-RO-BRR-045",
        "Tapacanto Barroco Ristretto Suave",
        "Soft",
        0.45,
        19,
        "Roble Barroco Ristretto",
        family="BARROCO_RISTRETTO",
    )
    board = client.get("/api/v1/products/code/MDP-RO-BRD-15").json()["data"]

    bands = client.get(f"/api/v1/products/{board['id']}/edge-bandings").json()["data"]
    # BARROCO_DORADO has no coordinated edge banding seeded; BARROCO_RISTRETTO must not leak in
    assert bands == []


def test_edge_bandings_excludes_inactive(client):
    """A discontinued edge banding must not be offered as coordinated."""
    _seed_cashmere_catalog(client)
    board = client.get("/api/v1/products/code/MDP-SL-CSH-15").json()["data"]
    soft = client.get("/api/v1/products/code/TAP-SL-CSH-045").json()["data"]

    client.put(f"/api/v1/products/{soft['id']}", json={"isActive": False})

    bands = client.get(f"/api/v1/products/{board['id']}/edge-bandings").json()["data"]
    assert [b["code"] for b in bands] == ["TAP-SL-CSH-150"]


def test_edge_bandings_invalid_band_type_returns_422(client):
    """The query param is closed to the BandType enum: an out-of-range value fails."""
    _seed_cashmere_catalog(client)
    board = client.get("/api/v1/products/code/MDP-SL-CSH-15").json()["data"]
    resp = client.get(
        f"/api/v1/products/{board['id']}/edge-bandings",
        params={"band_type": "Medio"},
    )
    assert resp.status_code == 422


def test_edge_banding_invalid_band_type_on_create_returns_422(client):
    """The band_type attribute is also closed to the enum when creating the product."""
    resp = client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": "TAP-XX-YY-045",
            "name": "Tapacanto inválido",
            "price": 1.0,
            "attributes": {"bandType": "Medio", "thickness": 0.45, "width": 19},
        },
    )
    assert resp.status_code == 422


def test_edge_bandings_board_not_found(client):
    assert client.get("/api/v1/products/999999/edge-bandings").status_code == 404


def test_edge_bandings_for_non_board_returns_business_rule_error(client):
    _seed_cashmere_catalog(client)
    edge = client.get("/api/v1/products/code/TAP-SL-CSH-045").json()["data"]

    resp = client.get(f"/api/v1/products/{edge['id']}/edge-bandings")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "BUSINESS_RULE_ERROR"
