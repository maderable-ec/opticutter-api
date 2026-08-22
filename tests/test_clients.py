"""Tests for the clients module (CRUD via CRUDService + thin routes)."""


def _payload(identifier="0991112233", first="Ada", last="Lovelace"):
    return {"identifier": identifier, "firstName": first, "lastName": last}


def test_create_and_get_client(client):
    resp = client.post("/api/v1/clients/", json=_payload())
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["identifier"] == "0991112233"
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
        "/api/v1/clients/", json=_payload(identifier="0990000001", first="Grace")
    )
    client.post(
        "/api/v1/clients/", json=_payload(identifier="0990000002", first="Alan")
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
    payload = {**_payload(), "phone": "0991112233", "email": "ada@example.com"}
    created = client.post("/api/v1/clients/", json=payload).json()["data"]
    assert created["phone"] == "0991112233"
    assert created["email"] == "ada@example.com"


def test_client_phone_and_email_are_optional_on_create(client):
    """The client is created fine without ``phone``/``email`` (the rule applies when quoting)."""
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    assert created["phone"] is None
    assert created["email"] is None


def test_update_client_phone(client):
    """``PUT`` allows registering the phone number later (e.g. once the client shares it)."""
    created = client.post("/api/v1/clients/", json=_payload()).json()["data"]
    resp = client.put(f"/api/v1/clients/{created['id']}", json={"phone": "0987654321"})
    assert resp.status_code == 200
    assert resp.json()["data"]["phone"] == "0987654321"
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
    for ident, last in [("1", "Zapata"), ("2", "Ayala"), ("3", "Moreno")]:
        client.post("/api/v1/clients/", json=_payload(identifier=ident, last=last))

    listed = client.get("/api/v1/clients/").json()["data"]
    assert [c["lastName"] for c in listed] == ["Ayala", "Moreno", "Zapata"]

    first = client.get("/api/v1/clients/", params={"limit": 2, "offset": 0}).json()
    second = client.get("/api/v1/clients/", params={"limit": 2, "offset": 2}).json()
    ids = [c["id"] for c in first["data"]] + [c["id"] for c in second["data"]]
    assert len(ids) == len(set(ids)) == 3


def test_list_clients_sort_recent_puts_the_newest_first(client):
    a = client.post("/api/v1/clients/", json=_payload(identifier="1")).json()["data"]
    b = client.post("/api/v1/clients/", json=_payload(identifier="2")).json()["data"]

    resp = client.get("/api/v1/clients/", params={"sort": "recent"}).json()
    assert [c["id"] for c in resp["data"]] == [b["id"], a["id"]]


def test_list_clients_search_still_narrows(client):
    client.post("/api/v1/clients/", json=_payload(identifier="1", last="Zapata"))
    client.post("/api/v1/clients/", json=_payload(identifier="2", last="Ayala"))

    found = client.get("/api/v1/clients/", params={"search": "ayala"}).json()
    assert [c["lastName"] for c in found["data"]] == ["Ayala"]
    assert found["meta"]["pagination"]["total"] == 1
