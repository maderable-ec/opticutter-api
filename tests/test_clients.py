"""Tests for the clients module (CRUD via CRUDService + thin routes)."""


def _payload(identifier="0100000397", first="Ada", last="Lovelace"):
    return {"identifier": identifier, "firstName": first, "lastName": last}


def test_create_and_get_client(client):
    resp = client.post("/api/v1/clients/", json=_payload())
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["identifier"] == "0100000397"
    assert created["firstName"] == "Ada"
    assert "id" in created

    got = client.get(f"/api/v1/clients/{created['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["id"] == created["id"]


def test_create_duplicate_identifier_returns_409(client):
    client.post("/api/v1/clients/", json=_payload())
    dup = client.post("/api/v1/clients/", json=_payload(first="Other"))
    assert dup.status_code == 409
    assert dup.json()["errors"][0]["message"] == "El identificador ya existe"


def test_get_missing_client_returns_404(client):
    resp = client.get("/api/v1/clients/999999")
    assert resp.status_code == 404
    error = resp.json()["errors"][0]
    assert error["code"] == "NOT_FOUND"
    assert "no encontrado" in error["message"]


def test_list_and_search_clients(client):
    client.post(
        "/api/v1/clients/", json=_payload(identifier="0100000033", first="Grace")
    )
    client.post(
        "/api/v1/clients/", json=_payload(identifier="0100000041", first="Alan")
    )

    listed = client.get("/api/v1/clients/")
    assert listed.status_code == 200
    body = listed.json()
    assert len(body["data"]) == 2
    assert body["meta"]["pagination"]["total"] == 2

    found = client.get("/api/v1/clients/", params={"search": "Grace"})
    assert found.status_code == 200
    names = [c["firstName"] for c in found.json()["data"]]
    assert names == ["Grace"]


def test_update_client(client):
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    resp = client.put(f"/api/v1/clients/{created['id']}", json={"firstName": "Augusta"})
    assert resp.status_code == 200
    assert resp.json()["data"]["firstName"] == "Augusta"
    assert resp.json()["data"]["lastName"] == "Lovelace"


def test_create_client_stores_phone_and_email(client):
    """``phone`` and ``email`` are stored and returned (email is optional)."""
    payload = {**_payload(), "phone": "0100000397", "email": "ada@example.com"}
    created = client.post("/api/v1/clients/", json=payload).json()["data"]
    assert created["phone"] == "0100000397"
    assert created["email"] == "ada@example.com"


def test_client_phone_and_email_are_optional_on_create(client):
    """The client is created fine without ``phone``/``email`` (the rule applies when quoting)."""
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    assert created["phone"] is None
    assert created["email"] is None


def test_update_client_phone(client):
    """``PUT`` allows registering the phone number later (e.g. once the client shares it)."""
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    resp = client.put(f"/api/v1/clients/{created['id']}", json={"phone": "0100000017"})
    assert resp.status_code == 200
    assert resp.json()["data"]["phone"] == "0100000017"
    # Doesn't overwrite the rest of the fields.
    assert resp.json()["data"]["firstName"] == "Ada"


def test_update_missing_client_returns_404(client):
    resp = client.put("/api/v1/clients/999999", json={"firstName": "Nobody"})
    assert resp.status_code == 404


def test_delete_client(client):
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    deleted = client.delete(f"/api/v1/clients/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/clients/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/clients/{created['id']}").status_code == 404


def test_list_clients_is_ordered_by_name_and_pages_do_not_overlap(client):
    """Paging is only safe with a total order: without it Postgres may repeat rows."""
    for ident, last in [
        ("0100000603", "Zapata"),
        ("0100000611", "Ayala"),
        ("0100000629", "Moreno"),
    ]:
        client.post("/api/v1/clients/", json=_payload(identifier=ident, last=last))

    listed = client.get("/api/v1/clients/").json()["data"]
    assert [c["lastName"] for c in listed] == ["Ayala", "Moreno", "Zapata"]

    first = client.get("/api/v1/clients/", params={"limit": 2, "offset": 0}).json()
    second = client.get("/api/v1/clients/", params={"limit": 2, "offset": 2}).json()
    ids = [c["id"] for c in first["data"]] + [c["id"] for c in second["data"]]
    assert len(ids) == len(set(ids)) == 3


def test_list_clients_sort_recent_puts_the_newest_first(client):
    a = client.post("/api/v1/clients/", json=_payload(identifier="0100000413")).json()[
        "data"
    ]
    b = client.post("/api/v1/clients/", json=_payload(identifier="0100000421")).json()[
        "data"
    ]

    resp = client.get("/api/v1/clients/", params={"sort": "recent"}).json()
    assert [c["id"] for c in resp["data"]] == [b["id"], a["id"]]


def test_list_clients_search_still_narrows(client):
    client.post(
        "/api/v1/clients/", json=_payload(identifier="0100000413", last="Zapata")
    )
    client.post(
        "/api/v1/clients/", json=_payload(identifier="0100000421", last="Ayala")
    )

    found = client.get("/api/v1/clients/", params={"search": "ayala"}).json()
    assert [c["lastName"] for c in found["data"]] == ["Ayala"]
    assert found["meta"]["pagination"]["total"] == 1


# --------------------------------------------------------------------------- #
# Cédula/RUC validation on the CRUD
# --------------------------------------------------------------------------- #


def test_create_rejects_a_mistyped_cedula(client):
    """Nine digits is a mistyped cédula, not another kind of document."""
    resp = client.post("/api/v1/clients/", json=_payload(identifier="010000039"))
    assert resp.status_code == 422


def test_create_rejects_a_wrong_check_digit(client):
    """The whole point of the algorithm: length alone accepts this."""
    good = "0100000397"
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert (
        client.post("/api/v1/clients/", json=_payload(identifier=bad)).status_code
        == 422
    )


def test_create_accepts_a_foreign_document(client):
    """A passport carries letters and can't be check-summed."""
    resp = client.post("/api/v1/clients/", json=_payload(identifier="AB123456"))
    assert resp.status_code == 201
    assert resp.json()["data"]["identifier"] == "AB123456"


def test_update_validates_the_identifier_too(client):
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    bad = client.put(
        f"/api/v1/clients/{created['id']}", json={"identifier": "010000039"}
    )
    assert bad.status_code == 422
    ok = client.put(f"/api/v1/clients/{created['id']}", json={"phone": "0100000397"})
    assert ok.status_code == 200


def test_a_client_stored_before_the_rule_is_still_readable(client, db_session):
    """``ClientResponse`` must not re-run the check: a row written by an older
    build (or by the seed) would otherwise 500 the listing, the order detail and
    the workshop board, none of which are editing anything."""
    from src.modules.clients.model import ClientModel

    db_session.add(ClientModel(identifier="9999999999", first_name="Consumidor"))
    db_session.commit()

    listed = client.get("/api/v1/clients/")
    assert listed.status_code == 200
    assert "9999999999" in [c["identifier"] for c in listed.json()["data"]]


# --------------------------------------------------------------------------- #
# Sync against the external client system (SIFAC)
# --------------------------------------------------------------------------- #

from sqlalchemy.exc import OperationalError  # noqa: E402

from src.modules.clients.external_clients import external_clients  # noqa: E402

# Real cédulas/RUCs, so the rows exercise the same split the live table does.
PERSON = "0105929863"
COMPANY = "1790941450001"


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
        self._records = records

    def connect(self):
        return _FakeConnection(self._records)


class _BrokenEngine:
    """Source is down: connecting raises the way SQLAlchemy would."""

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def _client_record(**overrides):
    base = {
        "ced": PERSON,
        "nom": "JHOANA PATRICIA GUAMAN VAZQUEZ",
        "mai": "jhoana@gmail.com",
        "tel": "0981105628",
        "tel2": None,
        "est": 1,
    }
    base.update(overrides)
    return base


def _load_clients(monkeypatch, *records):
    """Points the shared source at these rows (same shape as ``isolated_cache``)."""
    monkeypatch.setattr(external_clients, "_engine", _FakeEngine(list(records)))
    monkeypatch.setattr(external_clients, "_initialized", True)


def _sync(client, dry_run=False):
    return client.post(f"/api/v1/clients/sync?dryRun={str(dry_run).lower()}")


def test_sync_imports_the_external_clients(client, monkeypatch):
    _load_clients(
        monkeypatch,
        _client_record(),
        _client_record(ced=COMPANY, nom="SOCIEDAD DE MADRES SALESIANAS", mai=None),
    )
    resp = _sync(client)
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "created": 2,
        "updated": 0,
        "skippedInvalid": 0,
        "skippedInactive": 0,
        "issues": [],
        "warnings": [],
        "dryRun": False,
    }

    listed = {c["identifier"]: c for c in client.get("/api/v1/clients/").json()["data"]}
    person = listed[PERSON]
    assert (person["firstName"], person["lastName"]) == (
        "JHOANA PATRICIA",
        "GUAMAN VAZQUEZ",
    )
    assert person["source"] == "sifac"
    # A company has no surname to split off.
    assert listed[COMPANY]["firstName"] == "SOCIEDAD DE MADRES SALESIANAS"
    assert listed[COMPANY]["lastName"] is None


def test_sync_reruns_update_instead_of_duplicating(client, monkeypatch):
    _load_clients(monkeypatch, _client_record())
    _sync(client)

    _load_clients(monkeypatch, _client_record(tel="0999999999"))
    again = _sync(client).json()["data"]
    assert (again["created"], again["updated"]) == (0, 1)

    listed = client.get("/api/v1/clients/").json()
    assert listed["meta"]["pagination"]["total"] == 1
    assert listed["data"][0]["phone"] == "0999999999"


def test_sync_reports_nothing_to_do_when_the_source_has_not_changed(
    client, monkeypatch
):
    """``updated`` counts real changes, so the modal can say "ya está al día"."""
    _load_clients(monkeypatch, _client_record())
    _sync(client)
    again = _sync(client).json()["data"]
    assert (again["created"], again["updated"]) == (0, 0)


def test_sync_never_blanks_a_field_the_source_does_not_have(client, monkeypatch):
    """SIFAC is the system of record, but 150 of its clients have no e-mail:
    mirroring that blank would delete what a seller typed into the dashboard."""
    _load_clients(monkeypatch, _client_record(mai=None, tel=None))
    _sync(client)
    created = client.get("/api/v1/clients/").json()["data"][0]
    client.put(
        f"/api/v1/clients/{created['id']}",
        json={"phone": "0100000397", "email": "escrito.a.mano@gmail.com"},
    )

    # The source still has no e-mail, but now has a phone.
    _load_clients(monkeypatch, _client_record(mai=None, tel="0100000017"))
    _sync(client)

    stored = client.get(f"/api/v1/clients/{created['id']}").json()["data"]
    assert stored["email"] == "escrito.a.mano@gmail.com"  # preserved
    assert stored["phone"] == "0100000017"  # the source won


def test_sync_never_overwrites_the_source_of_a_hand_created_client(client, monkeypatch):
    created = client.post("/api/v1/clients/", json=_payload(identifier=PERSON)).json()[
        "data"
    ]
    assert created["source"] is None

    _load_clients(monkeypatch, _client_record())
    _sync(client)

    stored = client.get(f"/api/v1/clients/{created['id']}").json()["data"]
    assert stored["source"] is None
    assert stored["firstName"] == "JHOANA PATRICIA"


def test_sync_skips_and_reports_an_unusable_cedula(client, monkeypatch):
    """These four are rows the live table actually holds."""
    _load_clients(
        monkeypatch,
        _client_record(),
        _client_record(ced="9999999999", nom="Consumidor final"),
        _client_record(ced="85230421", nom="FELIX SEGUNDO POLO PARDO"),
        _client_record(ced="1400048947", nom="PAOLA KAJEKAI"),
    )
    data = _sync(client).json()["data"]
    assert data["created"] == 1
    assert data["skippedInvalid"] == 3
    # The report has to carry what is searched for in the external system.
    assert {i["code"] for i in data["issues"]} == {
        "9999999999",
        "85230421",
        "1400048947",
    }
    assert client.get("/api/v1/clients/").json()["meta"]["pagination"]["total"] == 1


def test_sync_warns_but_still_imports_a_client_with_a_broken_field(client, monkeypatch):
    _load_clients(monkeypatch, _client_record(tel="N/A", mai="nelusata04gmail.com"))
    data = _sync(client).json()["data"]
    assert (data["created"], data["skippedInvalid"]) == (1, 0)
    assert len(data["warnings"]) == 2

    stored = client.get("/api/v1/clients/").json()["data"][0]
    assert stored["phone"] is None and stored["email"] is None


def test_sync_counts_the_rows_the_source_retired(client, monkeypatch):
    _load_clients(monkeypatch, _client_record(), _client_record(ced=COMPANY, est=0))
    data = _sync(client).json()["data"]
    assert (data["created"], data["skippedInactive"]) == (1, 1)


def test_sync_never_removes_a_client_the_source_stopped_bringing(client, monkeypatch):
    """Orders and pre-orders point at clients — a sync must not delete one."""
    _load_clients(
        monkeypatch, _client_record(), _client_record(ced=COMPANY, nom="ACME")
    )
    _sync(client)

    _load_clients(monkeypatch, _client_record())
    data = _sync(client).json()["data"]
    assert data["created"] == 0
    assert client.get("/api/v1/clients/").json()["meta"]["pagination"]["total"] == 2


def test_sync_dry_run_reports_without_writing(client, monkeypatch):
    _load_clients(
        monkeypatch, _client_record(), _client_record(ced=COMPANY, nom="ACME")
    )

    preview = _sync(client, dry_run=True).json()["data"]
    assert preview["created"] == 2 and preview["dryRun"] is True
    assert client.get("/api/v1/clients/").json()["meta"]["pagination"]["total"] == 0

    applied = _sync(client).json()["data"]
    # The preview is what the operator approves, so it has to be what happens.
    assert applied["created"] == preview["created"]
    assert client.get("/api/v1/clients/").json()["meta"]["pagination"]["total"] == 2


def test_sync_fails_loudly_when_the_source_is_unreachable(client, monkeypatch):
    monkeypatch.setattr(external_clients, "_engine", _BrokenEngine())
    monkeypatch.setattr(external_clients, "_initialized", True)
    resp = _sync(client)
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["code"] == "EXTERNAL_SERVICE_ERROR"


def test_sync_aborts_when_the_source_returns_nothing_active(client, monkeypatch):
    """A broken filter, an empty table or the wrong database — never legitimate."""
    _load_clients(monkeypatch, _client_record(est=0))
    resp = _sync(client)
    assert resp.status_code == 422
    assert client.get("/api/v1/clients/").json()["meta"]["pagination"]["total"] == 0
