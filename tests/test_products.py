"""Tests for the products module (multi-type catalog: CRUD + per-type validation)."""

from decimal import Decimal

from sqlalchemy.exc import OperationalError

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.products.external_catalog import external_catalog


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


# --------------------------------------------------------------------------- #
# subtype
# --------------------------------------------------------------------------- #
def test_subtype_round_trips_and_filters(client):
    payload = _board_payload()
    payload["attributes"]["subtype"] = "mdp"  # case-insensitive input
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "MDP"

    other = client.post(
        "/api/v1/products/", json=_board_payload(code="MEL15", name="Otro")
    ).json()["data"]
    assert other["attributes"].get("subtype") is None

    matched = client.get("/api/v1/products/?subtype=mdp").json()["data"]
    assert [p["id"] for p in matched] == [created["id"]]

    empty = client.get("/api/v1/products/?subtype=osb").json()["data"]
    assert empty == []


def test_board_subtype_spanish_aliases_normalize_to_english(client):
    """The vendor's Spanish TIPO text (e.g. from catalog_sync) must still
    resolve, normalized to the canonical English value."""
    payload = _board_payload(code="PIN2", name="Pino board")
    payload["attributes"]["subtype"] = "Pino"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "Pine"

    payload = _board_payload(code="ENC1", name="Enchapado board")
    payload["attributes"]["subtype"] = "Enchapado"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "Veneer"


def test_edge_banding_subtype_round_trips(client):
    payload = _edge_banding_payload()
    payload["attributes"]["subtype"] = "Canto Solido"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "Solid"


def test_edge_banding_subtype_spanish_aliases_normalize_to_english(client):
    payload = _edge_banding_payload(code="TAP23", name="Tapacanto madera")
    payload["attributes"]["subtype"] = "Canto Madera"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "Wood"

    payload = _edge_banding_payload(code="TAP24", name="Tapacanto gloss")
    payload["attributes"]["subtype"] = "Canto Gloss"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["subtype"] == "Gloss"


# --------------------------------------------------------------------------- #
# alias (edge banding only)
# --------------------------------------------------------------------------- #
def test_edge_banding_alias_is_independent_of_family(client):
    payload = _edge_banding_payload()
    payload["attributes"]["family"] = "Cashmere"
    payload["attributes"]["alias"] = "CSH"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert created["attributes"]["family"] == "Cashmere"
    assert created["attributes"]["alias"] == "CSH"


def test_board_has_no_alias_attribute(client):
    """``alias`` isn't a `BoardAttributes` field — sending it is silently dropped."""
    payload = _board_payload()
    payload["attributes"]["alias"] = "CSH"
    created = client.post("/api/v1/products/", json=payload).json()["data"]
    assert "alias" not in created["attributes"]


# --------------------------------------------------------------------------- #
# External inventory sync
#
# The vendor's MySQL is swapped for canned ``marticulo`` rows, so the whole
# pipeline runs for real — column mapping, active/retired split, validation,
# upsert and reconciliation — without a live external server.
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, records):
        self._records = records

    def mappings(self):
        return self

    def all(self):
        return self._records


class _FakeConnection:
    def __init__(self, records):
        self._records = records

    def execute(self, _query):
        return _FakeResult(self._records)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, records):
        self.records = records

    def connect(self):
        return _FakeConnection(self.records)


def _load_inventory(monkeypatch, *records):
    """Points the shared source at these rows (same shape as ``isolated_cache``)."""
    monkeypatch.setattr(external_catalog, "_engine", _FakeEngine(list(records)))
    monkeypatch.setattr(external_catalog, "_initialized", True)


def _board_record(**overrides):
    record = {
        "cin": "1033",
        "nom": "MDP RH ROBLE BARROCO AMBAR (2.07X2.80)M-15MM",
        "mar": "KRONOSPAN",
        "tip": "MDP",
        "cat": "TABLEROS",
        "gru": "MDP MELAMINA RH",
        "iva": Decimal("15.00"),
        "ven": Decimal("14.650000"),
        "obs": "CSH - Cashmere",
        "est": 1,
        "FecEli": None,
    }
    record.update(overrides)
    return record


def _edge_record(**overrides):
    record = {
        "cin": "57",
        "nom": "TAPACANTO IBIZA 19X0.40MM",
        "mar": "HF",
        "tip": "TAPACANTOS",
        "cat": "TAPACANTOS",
        "gru": "CANTO MADERADO",
        "iva": Decimal("15.00"),
        "ven": Decimal("0.350000"),
        "obs": "CSH - Cashmere",
        "est": 1,
        "FecEli": None,
    }
    record.update(overrides)
    return record


_MEDIO_RECORD = _board_record(
    cin=9999,
    nom="MDP RH IBIZA (2.15X2.44)M-15MM (MEDIO)",
    mar="DURATEX",
    ven=Decimal("44.130000"),
    obs="",
)

_OTHER_BOARD_RECORD = _board_record(
    cin=9001,
    nom="MDP RH OTRO DISEÑO (1.83X2.44)M-15MM",
    obs="",
)


def _sync(client, dry_run=False):
    return client.post(f"/api/v1/products/sync?dryRun={str(dry_run).lower()}")


def _issues(resp):
    return resp.json()["data"]["issues"]


def test_sync_happy_path_creates_board_and_edge_banding(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    resp = _sync(client)
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data == {
        "created": 2,
        "updated": 0,
        "deactivated": 0,
        "deleted": 0,
        "skippedMedio": 0,
        "skippedInactive": 0,
        "skippedInvalid": 0,
        "issues": [],
        "dryRun": False,
    }

    listed = client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"]
    assert len(listed) == 1
    board = listed[0]
    assert board["type"] == "board"
    assert board["code"] == "1033"
    assert board["externalCode"] == "TABLEROS:1033"
    # `ven` (14.65) comes without IVA; the catalog stores the price with the
    # row's own IVA rate (15%) already included: 14.65 * 1.15 = 16.8475 -> 16.85.
    assert board["price"] == 16.85
    assert board["attributes"]["width"] == 2070
    assert board["attributes"]["height"] == 2800
    assert board["attributes"]["thickness"] == 15
    assert board["attributes"]["subtype"] == "MDP"
    assert board["attributes"]["family"] == "Cashmere"
    assert "alias" not in board["attributes"]

    eb = client.get("/api/v1/products/?search=IBIZA").json()["data"][0]
    assert eb["attributes"]["width"] == 19
    assert eb["attributes"]["thickness"] == 0.40
    assert eb["attributes"]["subtype"] == "Wood Grain"
    assert eb["attributes"]["family"] == "Cashmere"
    assert eb["attributes"]["alias"] == "CSH"
    # No band-type column in the vendor's schema: inferred from thickness
    # (<1mm = Soft).
    assert eb["attributes"]["bandType"] == "Soft"


def test_sync_infers_hard_band_type_from_thickness(client, monkeypatch):
    _load_inventory(monkeypatch, _edge_record(nom="TAPACANTO IBIZA 40X1.5MM"))
    _sync(client)
    eb = client.get("/api/v1/products/?search=IBIZA").json()["data"][0]
    assert eb["attributes"]["thickness"] == 1.5
    assert eb["attributes"]["bandType"] == "Hard"


def test_sync_skips_medio_row(client, monkeypatch):
    _load_inventory(monkeypatch, _MEDIO_RECORD)
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["skippedMedio"] == 1
    assert resp.json()["data"]["created"] == 0
    assert client.get("/api/v1/products/").json()["data"] == []


def test_sync_skips_a_duplicate_codigo_and_keeps_the_first(client, monkeypatch):
    _load_inventory(
        monkeypatch,
        _edge_record(),
        _edge_record(nom="TAPACANTO KALA WHITE 19X1.5MM"),
    )
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["skippedInvalid"] == 1
    assert any("duplicado" in i["message"] for i in _issues(resp))


def test_sync_skips_a_codigo_shared_between_board_and_edge_banding(client, monkeypatch):
    """`code` is the bare CODIGO (not namespaced by categoría), so a board and
    an edge banding sharing the same CODIGO would collide on `code` even
    though their `external_code`s differ ("TABLEROS:1033" vs "TAPACANTOS:1033")."""
    _load_inventory(monkeypatch, _board_record(), _edge_record(cin=1033))
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert any("duplicado" in i["message"] for i in _issues(resp))


def test_sync_skips_unparseable_dimensions_and_imports_the_rest(client, monkeypatch):
    """The real inventory carries articles whose name has no usable dimensions
    at all ("LIBRE"). Those can never be parsed, so they must not be able to
    hold back the hundreds of articles that are perfectly readable."""
    _load_inventory(
        monkeypatch,
        _board_record(),
        _board_record(
            cin=2026,
            nom="PLYWOOD ESTANDAR (244X122)X4MM",
            tip="PLYWOOD",
            gru="ESTANDAR",
        ),
    )
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["skippedInvalid"] == 1
    assert any("medidas" in i["message"] for i in _issues(resp))
    assert len(client.get("/api/v1/products/").json()["data"]) == 1


def test_sync_skips_a_malformed_price(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(ven=None))
    resp = _sync(client)
    assert resp.status_code == 200
    assert any("precio de venta" in i["message"] for i in _issues(resp))


def test_sync_skips_a_malformed_iva(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(iva="quince"))
    resp = _sync(client)
    assert resp.status_code == 200
    assert any("IVA" in i["message"] for i in _issues(resp))


def test_sync_skips_an_unrecognized_category(client, monkeypatch):
    _load_inventory(
        monkeypatch, _board_record(), _board_record(cin=8080, cat="HERRAJES")
    )
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert any("no reconocida" in i["message"] for i in _issues(resp))


def test_sync_refuses_to_steal_a_hand_created_products_name(client, monkeypatch):
    """Hand-created products are never touched, so a name collision has to be
    reported rather than resolved — otherwise the unique constraint would."""
    client.post(
        "/api/v1/products/",
        json=_board_payload(code="MANUAL1", name=_board_record()["nom"]),
    )
    _load_inventory(monkeypatch, _board_record())
    resp = _sync(client)
    assert resp.status_code == 422
    assert any("ya lo usa otro producto" in e["message"] for e in resp.json()["errors"])


def test_sync_refuses_to_steal_a_hand_created_products_code(client, monkeypatch):
    client.post(
        "/api/v1/products/", json=_board_payload(code="1033", name="Hecho a mano")
    )
    _load_inventory(monkeypatch, _board_record())
    resp = _sync(client)
    assert resp.status_code == 422
    assert any("ya lo usa otro producto" in e["message"] for e in resp.json()["errors"])


def test_sync_skips_a_name_used_twice_in_the_source(client, monkeypatch):
    """`name` is unique in the catalog, so of two vendor articles sharing one
    only the first is imported; the second is reported."""
    _load_inventory(monkeypatch, _board_record(), _board_record(cin=8081))
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert any("duplicado" in i["message"] for i in _issues(resp))


def test_sync_imports_a_board_with_fractional_thickness(client, monkeypatch):
    """Real article from the vendor's table. The name always parsed; what
    rejected it was the catalog's own integer-only thickness field."""
    _load_inventory(monkeypatch, _board_record(nom="MDF RH (2.44X2.13)M-5.5MM"))
    resp = _sync(client)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["skippedInvalid"] == 0

    board = client.get("/api/v1/products/?search=MDF RH").json()["data"][0]
    assert board["attributes"]["thickness"] == 5.5


def test_sync_skips_a_board_with_impossible_attributes(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(nom="MDP RH PRUEBA (2.07X2.80)M-0MM"))
    resp = _sync(client)
    assert resp.status_code == 200
    assert any("tablero inválidos" in i["message"] for i in _issues(resp))


def test_sync_skips_an_edge_banding_with_unparseable_dimensions(client, monkeypatch):
    # Real shape from the vendor's table: the "MM" suffix is simply missing.
    _load_inventory(monkeypatch, _edge_record(nom="TAPACANTO CAPRI 22X1.5"))
    resp = _sync(client)
    assert resp.status_code == 200
    assert any("medidas" in i["message"] for i in _issues(resp))


def test_sync_skips_an_edge_banding_with_impossible_attributes(client, monkeypatch):
    _load_inventory(monkeypatch, _edge_record(nom="TAPACANTO CAPRI 0X0.40MM"))
    resp = _sync(client)
    assert resp.status_code == 200
    assert any("tapacanto inválidos" in i["message"] for i in _issues(resp))


def test_sync_issue_identifies_the_row_by_vendor_code_and_name(client, monkeypatch):
    """There's no file to count lines in any more — the operator finds the row
    in the inventory system by its code and article name."""
    _load_inventory(
        monkeypatch,
        _board_record(),
        _board_record(cin=4242, nom="TABLERO SIN MEDIDAS RECONOCIBLES"),
    )
    resp = _sync(client)
    assert resp.status_code == 200
    (issue,) = _issues(resp)
    assert issue["code"] == "4242"
    assert issue["name"] == "TABLERO SIN MEDIDAS RECONOCIBLES"


def test_sync_reruns_update_instead_of_duplicating(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record())
    first = _sync(client).json()["data"]
    assert first["created"] == 1

    _load_inventory(monkeypatch, _board_record(ven=Decimal("20.000000")))
    second = _sync(client).json()["data"]
    assert second["created"] == 0
    assert second["updated"] == 1
    assert second["deleted"] == 0

    listed = client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"]
    assert len(listed) == 1
    assert listed[0]["price"] == 23.0  # 20.00 * 1.15 IVA


def test_sync_deletes_unused_products_missing_from_a_later_read(client, monkeypatch):
    """A synced product with no order referencing it is deleted outright,
    not just deactivated, when it drops out of a later read."""
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    _sync(client)

    # Only the edge banding comes back this time; the board type isn't touched
    # at all (no TABLEROS rows in this read).
    _load_inventory(monkeypatch, _edge_record())
    resp = _sync(client)
    assert resp.json()["data"]["updated"] == 1
    assert resp.json()["data"]["deleted"] == 0
    assert resp.json()["data"]["deactivated"] == 0

    # A read that DOES include the board type, but omits this specific board
    # (never referenced by any order), deletes it outright.
    _load_inventory(monkeypatch, _OTHER_BOARD_RECORD)
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["deactivated"] == 0
    assert resp.json()["data"]["deleted"] == 1

    assert client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"] == []


def test_sync_keeps_a_product_whose_row_stopped_being_readable(client, monkeypatch):
    """The trap of skip-and-report: a row we failed to parse is missing from
    the valid set, which is exactly what reconciliation deletes on. "We
    couldn't read it" must not be confused with "the vendor removed it"."""
    _load_inventory(monkeypatch, _board_record(), _OTHER_BOARD_RECORD)
    assert _sync(client).json()["data"]["created"] == 2

    # Someone edits the article in the inventory and breaks its dimensions.
    _load_inventory(
        monkeypatch,
        _board_record(nom="MDP RH ROBLE BARROCO AMBAR SIN MEDIDAS"),
        _OTHER_BOARD_RECORD,
    )
    data = _sync(client).json()["data"]
    assert data["skippedInvalid"] == 1
    assert data["deleted"] == 0
    assert data["deactivated"] == 0

    survivor = client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"]
    assert len(survivor) == 1
    assert survivor[0]["isActive"] is True


def test_sync_still_aborts_when_the_source_clashes_with_the_catalog(
    client, monkeypatch
):
    """Unreadable data is the source's problem and gets skipped; a name or code
    already owned by a hand-created product is the destination's, and importing
    over it would corrupt a catalog nobody asked to change."""
    client.post(
        "/api/v1/products/",
        json=_board_payload(code="MANUAL9", name=_board_record()["nom"]),
    )
    _load_inventory(monkeypatch, _board_record(), _OTHER_BOARD_RECORD)
    resp = _sync(client)
    assert resp.status_code == 422
    # Zero writes: the good row alongside it isn't imported either.
    assert client.get("/api/v1/products/?search=OTRO DISE").json()["data"] == []


def test_sync_retires_products_the_vendor_flagged_inactive(client, monkeypatch):
    """`est`/`FecEli` make the retirement explicit: the row still comes back,
    it just no longer counts as part of the catalog."""
    _load_inventory(monkeypatch, _board_record(), _OTHER_BOARD_RECORD)
    assert _sync(client).json()["data"]["created"] == 2

    _load_inventory(monkeypatch, _board_record(est=0), _OTHER_BOARD_RECORD)
    data = _sync(client).json()["data"]
    assert data["skippedInactive"] == 1
    assert data["updated"] == 1
    assert data["deleted"] == 1

    assert client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"] == []


def test_sync_ignores_malformed_data_on_a_retired_row(client, monkeypatch):
    """A retired article never reaches validation, so bad data on something
    nobody sells any more can't abort the whole sync."""
    _load_inventory(
        monkeypatch,
        _board_record(),
        _board_record(cin=7777, nom="CHATARRA SIN FORMATO", est=0),
    )
    resp = _sync(client)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["data"]["created"] == 1
    assert resp.json()["data"]["skippedInactive"] == 1


def test_sync_aborts_when_the_source_returns_nothing_active(client, monkeypatch):
    """Reconciliation removes whatever the read didn't bring, so an empty read
    would wipe the catalog — that's a broken query, never a real catalog."""
    _load_inventory(monkeypatch, _board_record())
    _sync(client)

    _load_inventory(monkeypatch, _board_record(est=0))
    resp = _sync(client)
    assert resp.status_code == 422
    assert client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"] != []


def test_sync_fails_loudly_when_the_source_is_unreachable(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record())
    _sync(client)

    class _BrokenEngine:
        def connect(self):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(external_catalog, "_engine", _BrokenEngine())
    monkeypatch.setattr(external_catalog, "_initialized", True)

    resp = _sync(client)
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["code"] == "EXTERNAL_SERVICE_ERROR"
    # The catalog is untouched: a dead source must never look like an empty one.
    assert client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"] != []


def test_sync_dry_run_reports_without_writing(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    preview = _sync(client, dry_run=True)
    assert preview.status_code == 200
    assert preview.json()["data"] == {
        "created": 2,
        "updated": 0,
        "deactivated": 0,
        "deleted": 0,
        "skippedMedio": 0,
        "skippedInactive": 0,
        "skippedInvalid": 0,
        "issues": [],
        "dryRun": True,
    }
    assert client.get("/api/v1/products/").json()["data"] == []

    # ...and the real run then does exactly what the preview announced.
    applied = _sync(client)
    assert applied.json()["data"]["created"] == 2
    assert len(client.get("/api/v1/products/").json()["data"]) == 2


def test_sync_dry_run_previews_deletions_without_applying_them(client, monkeypatch):
    _load_inventory(monkeypatch, _board_record(), _OTHER_BOARD_RECORD)
    _sync(client)

    _load_inventory(monkeypatch, _OTHER_BOARD_RECORD)
    preview = _sync(client, dry_run=True).json()["data"]
    assert preview["deleted"] == 1
    assert preview["dryRun"] is True
    # Still there: the pass ran and rolled back.
    assert client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"] != []


def _mint_order_referencing(client, db_session, board_id, eb_id):
    """Creates a real order whose cutting plan durably references ``board_id``
    (via order_boards/order_pieces) and ``eb_id`` (via order_lines' billed
    edge-banding), so ``_is_product_in_use`` finds it."""
    c = client.post(
        "/api/v1/clients/",
        json={
            "identifier": "0991112233",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": "0991112233",
        },
    ).json()["data"]
    return OrderService(db_session).create(
        OrderCreate.model_validate(
            {
                "clientId": c["id"],
                "branchId": 1,
                "materials": [
                    {"key": "b1", "source": "catalog", "productId": board_id}
                ],
                "requirements": [
                    {
                        "priority": 0,
                        "height": 500,
                        "width": 1000,
                        "quantity": 1,
                        "materialKey": "b1",
                        "label": "Costado",
                        "edgeBanding": {"productId": eb_id, "sides": ["left"]},
                    }
                ],
            }
        )
    )


def test_sync_deactivates_products_in_use_missing_from_a_later_read(
    client, db_session, monkeypatch
):
    """A synced product an order actually references gets deactivated instead
    of deleted — the FK would reject the delete."""
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    _sync(client)
    board = client.get("/api/v1/products/?search=ROBLE BARROCO").json()["data"][0]
    eb = client.get("/api/v1/products/?search=IBIZA").json()["data"][0]
    _mint_order_referencing(client, db_session, board["id"], eb["id"])

    _load_inventory(monkeypatch, _OTHER_BOARD_RECORD)
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["deactivated"] == 1
    assert resp.json()["data"]["deleted"] == 0

    original = client.get(f"/api/v1/products/{board['id']}").json()["data"]
    assert original["isActive"] is False


def test_sync_never_deactivates_a_hand_created_product(client, monkeypatch):
    manual = client.post("/api/v1/products/", json=_board_payload()).json()["data"]

    _load_inventory(monkeypatch, _board_record())
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"]["deactivated"] == 0

    still_active = client.get(f"/api/v1/products/{manual['id']}").json()["data"]
    assert still_active["isActive"] is True


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


def test_list_filter_by_multiple_types(client):
    """Repeating the ``type`` param (multi-select) OR-matches within the field."""
    client.post("/api/v1/products/", json=_board_payload(code="MEL18", name="Blanco"))
    client.post("/api/v1/products/", json=_edge_banding_payload())

    both = client.get(
        "/api/v1/products/", params={"type": ["board", "edge_banding"]}
    ).json()
    assert both["meta"]["pagination"]["total"] == 2
    assert {p["type"] for p in both["data"]} == {"board", "edge_banding"}

    # A single repeated value still behaves like the old single-value filter.
    boards = client.get("/api/v1/products/", params={"type": ["board"]}).json()
    assert boards["meta"]["pagination"]["total"] == 1


def test_list_filter_by_multiple_subtypes(client):
    """Repeating the ``subtype`` param (multi-select) OR-matches within the field."""
    mdp = _board_payload(code="MEL18", name="Blanco")
    mdp["attributes"]["subtype"] = "MDP"
    client.post("/api/v1/products/", json=mdp)

    osb = _board_payload(code="MEL19", name="Beige")
    osb["attributes"]["subtype"] = "OSB"
    client.post("/api/v1/products/", json=osb)

    client.post("/api/v1/products/", json=_board_payload(code="MEL20", name="Gris"))

    matched = client.get("/api/v1/products/", params={"subtype": ["mdp", "osb"]}).json()
    assert matched["meta"]["pagination"]["total"] == 2
    assert {p["code"] for p in matched["data"]} == {"MEL18", "MEL19"}


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

    # camelCase on the wire, like every other filter in the API (the body already
    # used `isActive`; only this query param was still snake_case).
    active = client.get("/api/v1/products/", params={"isActive": True}).json()
    assert [p["code"] for p in active["data"]] == ["ON"]
    inactive = client.get("/api/v1/products/", params={"isActive": False}).json()
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
