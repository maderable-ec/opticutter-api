"""Tests for the projects module: the designer's projects and the seller's quote.

The designer (``proyectista``) draws a cut list with the optimizer, saves it as a
project and hands it to the sellers (``ready``); a seller quotes it by creating
a pre-order with ``projectId``, which marks the project ``quoted``.
"""

from .order_helpers import _create_board, _create_client, _token_for
from .test_orders import _order_payload

# The optimizer's form state as the frontend sends it, with a half-filled row:
# the backend stores it without validating its shape, as it does a draft's.
FORM_PAYLOAD = {
    "version": 1,
    "materials": [{"uid": "mat-0-abc", "source": "catalog", "boardId": "12"}],
    "requirements": [
        {"materialUid": "mat-0-abc", "height": 720, "width": 400, "quantity": 2},
        {"materialUid": "mat-0-abc", "height": 500, "width": "", "quantity": 1},
    ],
}


def _designer(client, db_session, email="proyectista@empresa.com"):
    return _token_for(client, db_session, "proyectista", email=email)


def _create(client, headers, name="Cocina Pérez", **extra):
    body = {"name": name, "payload": FORM_PAYLOAD, **extra}
    return client.post("/api/v1/projects/", json=body, headers=headers)


def _status(client, headers, pid, status):
    return client.patch(
        f"/api/v1/projects/{pid}/status", json={"status": status}, headers=headers
    )


def _ready_project(client, db_session):
    designer = _designer(client, db_session)
    pid = _create(client, designer).json()["data"]["id"]
    assert _status(client, designer, pid, "ready").status_code == 200
    return pid


def test_the_designer_creates_a_project_they_own(client, db_session):
    designer = _designer(client, db_session)
    resp = _create(client, designer, clientName="Sra. Pérez", notes="Isla al centro")
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["status"] == "in_progress"
    assert data["clientName"] == "Sra. Pérez"
    assert data["notes"] == "Isla al centro"
    assert data["ownerName"] == "Proyectista"
    assert data["readyAt"] is None
    assert data["quotes"] == []

    got = client.get(f"/api/v1/projects/{data['id']}", headers=designer)
    assert got.status_code == 200
    # The bag is kept as-is, half-filled row included.
    assert got.json()["data"]["payload"] == FORM_PAYLOAD
    assert got.json()["data"]["ownerId"] == data["ownerId"]


def test_a_designer_sees_only_their_own_projects(client, db_session):
    mine = _designer(client, db_session)
    other = _designer(client, db_session, email="otra-proyectista@empresa.com")
    pid = _create(client, mine).json()["data"]["id"]
    _create(client, other, name="Closet")

    listing = client.get("/api/v1/projects/", headers=other).json()
    assert [p["name"] for p in listing["data"]] == ["Closet"]
    # Another's project does not exist for them: a uniform 404, never a 403.
    assert client.get(f"/api/v1/projects/{pid}", headers=other).status_code == 404
    put = client.put(f"/api/v1/projects/{pid}", json={"name": "X"}, headers=other)
    assert put.status_code == 404
    assert _status(client, other, pid, "ready").status_code == 404


def test_the_seller_reads_every_project_but_writes_none(client, db_session):
    designer = _designer(client, db_session)
    pid = _create(client, designer).json()["data"]["id"]
    seller = _token_for(client, db_session, "vendedor")

    listing = client.get("/api/v1/projects/", headers=seller)
    assert listing.status_code == 200
    assert [p["id"] for p in listing.json()["data"]] == [pid]
    assert client.get(f"/api/v1/projects/{pid}", headers=seller).status_code == 200

    assert _create(client, seller).status_code == 403
    put = client.put(f"/api/v1/projects/{pid}", json={"name": "X"}, headers=seller)
    assert put.status_code == 403
    assert _status(client, seller, pid, "ready").status_code == 403


def test_the_shop_floor_has_no_access(client, db_session):
    operator = _token_for(client, db_session, "operador")
    assert client.get("/api/v1/projects/", headers=operator).status_code == 403


def test_a_project_is_edited_only_while_in_progress(client, db_session):
    designer = _designer(client, db_session)
    pid = _create(client, designer).json()["data"]["id"]
    _status(client, designer, pid, "ready")

    refused = client.put(
        f"/api/v1/projects/{pid}", json={"name": "Otra"}, headers=designer
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["field"] == "status"

    assert _status(client, designer, pid, "in_progress").status_code == 200
    edited = client.put(
        f"/api/v1/projects/{pid}",
        json={"name": "Otra", "clientName": None, "payload": None},
        headers=designer,
    )
    assert edited.status_code == 200
    data = edited.json()["data"]
    assert data["name"] == "Otra"
    # An explicit null on a required column leaves it; on an optional one, clears it.
    assert data["payload"] == FORM_PAYLOAD
    assert data["clientName"] is None


def test_the_status_graph(client, db_session):
    designer = _designer(client, db_session)
    pid = _create(client, designer).json()["data"]["id"]

    ready = _status(client, designer, pid, "ready")
    assert ready.status_code == 200
    assert ready.json()["data"]["readyAt"] is not None
    # Idempotent: the same status again is a no-op.
    assert _status(client, designer, pid, "ready").status_code == 200

    assert _status(client, designer, pid, "archived").status_code == 200
    # Archived only reopens; it is not handed to the sellers straight away.
    jump = _status(client, designer, pid, "ready")
    assert jump.status_code == 422
    assert jump.json()["errors"][0]["field"] == "status"
    assert _status(client, designer, pid, "in_progress").status_code == 200

    # ``quoted`` is never a PATCH target: only a pre-order sets it.
    assert _status(client, designer, pid, "quoted").status_code == 422


def test_listing_filters(client, db_session):
    designer = _designer(client, db_session)
    other = _designer(client, db_session, email="otra-proyectista@empresa.com")
    kitchen = _create(client, designer, clientName="Sra. Pérez").json()["data"]
    _create(client, designer, name="Closet")
    theirs = _create(client, other, name="Baño").json()["data"]
    _status(client, designer, kitchen["id"], "ready")

    def names(**params):
        resp = client.get("/api/v1/projects/", params=params)  # as admin
        assert resp.status_code == 200
        return sorted(p["name"] for p in resp.json()["data"])

    assert names() == ["Baño", "Closet", "Cocina Pérez"]
    assert names(status="ready") == ["Cocina Pérez"]
    assert names(status=["ready", "in_progress"]) == ["Baño", "Closet", "Cocina Pérez"]
    assert names(search="pérez") == ["Cocina Pérez"]
    assert names(ownerId=theirs["ownerId"]) == ["Baño"]
    # A designer is locked to their own, whatever ownerId says.
    scoped = client.get(
        "/api/v1/projects/", params={"ownerId": theirs["ownerId"]}, headers=designer
    )
    assert sorted(p["name"] for p in scoped.json()["data"]) == [
        "Closet",
        "Cocina Pérez",
    ]


def test_the_designer_works_in_the_optimizer_but_not_in_sales(client, db_session):
    designer = _designer(client, db_session)
    for path in ("/api/v1/optimize/cut-rules", "/api/v1/products/"):
        assert client.get(path, headers=designer).status_code == 200, path
    for path in (
        "/api/v1/optimization-drafts/",
        "/api/v1/clients/",
        "/api/v1/preorders/",
        "/api/v1/orders/",
    ):
        assert client.get(path, headers=designer).status_code == 403, path
    # The seller keeps the branch drafts.
    seller = _token_for(client, db_session, "vendedor")
    assert client.get("/api/v1/optimization-drafts/", headers=seller).status_code == 200


def test_a_seller_quotes_a_ready_project(client, db_session):
    pid = _ready_project(client, db_session)
    seller = _token_for(client, db_session, "vendedor")
    c, b = _create_client(client), _create_board(client)

    quote = client.post(
        "/api/v1/preorders/",
        json={**_order_payload(c["id"], b["id"]), "projectId": pid},
        headers=seller,
    )
    assert quote.status_code == 201
    assert quote.json()["data"]["projectId"] == pid

    project = client.get(f"/api/v1/projects/{pid}").json()["data"]
    assert project["status"] == "quoted"
    assert project["quotes"] == [
        {
            "id": quote.json()["data"]["id"],
            "code": quote.json()["data"]["code"],
            "status": "draft",
        }
    ]
    listing = client.get("/api/v1/preorders/", headers=seller).json()["data"]
    assert listing[0]["projectId"] == pid
    # The sellers' queue shows the quote without opening the project.
    queue = client.get("/api/v1/projects/", headers=seller).json()["data"]
    assert queue[0]["quotes"] == project["quotes"]

    # A quoted project is read-only for its designer...
    designer = _designer(client, db_session)
    assert _status(client, designer, pid, "in_progress").status_code == 422
    # ...and can be quoted again (a rejected quote is re-quoted from it).
    again = client.post(
        "/api/v1/preorders/",
        json={**_order_payload(c["id"], b["id"], width=600), "projectId": pid},
        headers=seller,
    )
    assert again.status_code == 201
    project = client.get(f"/api/v1/projects/{pid}").json()["data"]
    assert project["status"] == "quoted"
    assert len(project["quotes"]) == 2


def test_only_a_ready_project_is_quoted(client, db_session):
    designer = _designer(client, db_session)
    pid = _create(client, designer).json()["data"]["id"]
    c, b = _create_client(client), _create_board(client)

    def quote():
        return client.post(
            "/api/v1/preorders/",
            json={**_order_payload(c["id"], b["id"]), "projectId": pid},
        )

    in_progress = quote()
    assert in_progress.status_code == 422
    assert in_progress.json()["errors"][0]["field"] == "projectId"

    _status(client, designer, pid, "archived")
    assert quote().status_code == 422
    # Refused before anything was written: no quote, and the project as it was.
    assert client.get("/api/v1/preorders/").json()["data"] == []
    assert client.get(f"/api/v1/projects/{pid}").json()["data"]["status"] == "archived"

    unknown = client.post(
        "/api/v1/preorders/",
        json={**_order_payload(c["id"], b["id"]), "projectId": 9999},
    )
    assert unknown.status_code == 404


def test_a_quote_without_a_project_is_unchanged(client):
    c, b = _create_client(client), _create_board(client)
    resp = client.post("/api/v1/preorders/", json=_order_payload(c["id"], b["id"]))
    assert resp.status_code == 201
    assert resp.json()["data"]["projectId"] is None
