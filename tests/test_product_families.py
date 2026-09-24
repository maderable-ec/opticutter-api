"""Design families: CRUD, bulk assignment, and the requirement the whole change
exists for — a local assignment must survive a resync.

The coordination between a board and its edge bandings used to be a free-text
key inside each product's ``attributes`` bag, seeded from the vendor's
``marticulo.obs``. It could not be managed from here for a mechanical reason:
the sync's update does ``product.attributes = row.attributes``, replacing the
whole bag, so anything typed into the product form was wiped on the next pass.
"""

from decimal import Decimal

import pytest

from tests.test_products import (  # reuse the fake inventory source
    _board_record,
    _edge_record,
    _load_inventory,
    _sync,
)


def _family(client, name="Cashmere", **extra):
    resp = client.post("/api/v1/product-families/", json={"name": name, **extra})
    assert resp.status_code == 201, resp.json()
    return resp.json()["data"]


def _board(client, code="MDP-15", family_id=None, thickness=15, name=None):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": name or f"Tablero {code}",
            "price": 50.0,
            "familyId": family_id,
            "attributes": {"height": 2800, "width": 2070, "thickness": thickness},
        },
    ).json()["data"]


def _band(client, code="TAP-19", family_id=None, width=19, alias="CSH", name=None):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": code,
            "name": name or f"Tapacanto {code}",
            "price": 2.0,
            "familyId": family_id,
            "alias": alias,
            "attributes": {"thickness": 0.45, "width": width, "bandType": "Soft"},
        },
    ).json()["data"]


# --------------------------------------------------------------------------- #
# The requirement: a local assignment survives the sync
# --------------------------------------------------------------------------- #
def test_a_hand_assigned_family_survives_a_resync(client, monkeypatch):
    """The whole point of the change, end to end.

    The vendor keeps saying "Cashmere" in OBS. The catalog admin moves the board
    to another design (the substitution case — 23 of the 134 MDP boards live
    like this) and retypes the tape's alias. A resync then brings fresh prices
    and names, and must leave both alone.
    """
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    _sync(client)

    board = client.get("/api/v1/products/code/1033").json()["data"]
    tape = client.get("/api/v1/products/code/57").json()["data"]
    seeded_family = board["familyId"]
    assert seeded_family is not None and tape["alias"] == "CSH"

    # The shop's own decision, made from the dashboard.
    olmo = _family(client, "Olmo Panela")
    assert (
        client.put(
            f"/api/v1/products/{board['id']}", json={"familyId": olmo["id"]}
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/products/{tape['id']}",
            json={"familyId": olmo["id"], "alias": "PAN"},
        ).status_code
        == 200
    )

    # The source has not changed its mind: OBS still says Cashmere.
    _load_inventory(
        monkeypatch,
        _board_record(
            ven=Decimal("19.900000"), nom="MDP RH ROBLE RENOVADO (2.80X2.07)M-15MM"
        ),
        _edge_record(),
    )
    resp = _sync(client)
    assert resp.json()["data"]["updated"] == 2

    board = client.get("/api/v1/products/code/1033").json()["data"]
    tape = client.get("/api/v1/products/code/57").json()["data"]

    # What WE own did not move...
    assert board["familyId"] == olmo["id"]
    assert tape["familyId"] == olmo["id"]
    assert tape["alias"] == "PAN"
    # ...and what the VENDOR owns did.
    assert board["price"] == 19.9
    assert board["name"] == "MDP RH ROBLE RENOVADO (2.80X2.07)M-15MM"

    # And the coordination actually works through the new family.
    bands = client.get(f"/api/v1/products/{board['id']}/edge-bandings").json()["data"]
    assert [b["code"] for b in bands] == ["57"]


def test_the_sync_seeds_a_family_only_when_it_creates_the_product(client, monkeypatch):
    """The mirror image: a brand-new article DOES take the family from OBS.

    Without this the change would just be "ignore the vendor", and every new
    article would arrive uncoordinated for somebody to assign by hand.
    """
    _load_inventory(monkeypatch, _board_record(), _edge_record())
    resp = _sync(client)
    assert resp.json()["data"]["familiesCreated"] == 1

    board = client.get("/api/v1/products/code/1033").json()["data"]
    assert board["family"]["name"] == "Cashmere"


def test_two_new_rows_of_one_design_do_not_duplicate_the_family(client, monkeypatch):
    """202 tapacantos share 72 designs.

    Without registering a just-created family before the next row is read, a
    first sync would try to insert the same design once per article and the
    UNIQUE constraint would abort the whole pass.
    """
    _load_inventory(
        monkeypatch,
        _edge_record(cin="57", obs="Cashmere - CSH"),
        _edge_record(cin="58", nom="TAPACANTO CASHMERE 22X1.5MM", obs="Cashmere - CSH"),
    )
    resp = _sync(client)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["data"]["familiesCreated"] == 1
    assert len(client.get("/api/v1/product-families/").json()["data"]) == 1


def test_the_family_key_is_case_and_space_insensitive_on_the_sync_too(
    client, monkeypatch
):
    """ "CASHMERE" and "  Cashmere " are one design, and the sync must agree with
    the dashboard about that or it would create a second row for the same name."""
    _family(client, "Cashmere")
    _load_inventory(
        monkeypatch,
        _board_record(obs="  CASHMERE "),
        _edge_record(obs="cashmere - CSH"),
    )
    resp = _sync(client)
    assert resp.json()["data"]["familiesCreated"] == 0
    assert len(client.get("/api/v1/product-families/").json()["data"]) == 1


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_and_read_a_family(client):
    created = _family(client, "Olmo Panela", description="Sustituye a Roble Barroco")
    assert created["name"] == "Olmo Panela"
    assert created["boardCount"] == 0 and created["edgeBandingCount"] == 0

    detail = client.get(f"/api/v1/product-families/{created['id']}").json()["data"]
    assert detail["description"] == "Sustituye a Roble Barroco"
    assert detail["boards"] == [] and detail["edgeBandings"] == []


@pytest.mark.parametrize("duplicate", ["Cashmere", "cashmere", "  CASHMERE  "])
def test_a_duplicate_name_is_a_conflict_whatever_its_casing(client, duplicate):
    _family(client, "Cashmere")
    resp = client.post("/api/v1/product-families/", json={"name": duplicate})
    assert resp.status_code == 409
    assert "ya existe" in resp.json()["errors"][0]["message"].lower()


def test_a_blank_name_is_rejected(client):
    resp = client.post("/api/v1/product-families/", json={"name": "   "})
    assert resp.status_code == 422


def test_renaming_a_family_keeps_its_members(client):
    fam = _family(client, "Cashmere")
    board = _board(client, family_id=fam["id"])
    client.put(f"/api/v1/product-families/{fam['id']}", json={"name": "Cashmere RH"})

    detail = client.get(f"/api/v1/product-families/{fam['id']}").json()["data"]
    assert detail["name"] == "Cashmere RH"
    assert [b["id"] for b in detail["boards"]] == [board["id"]]
    # Renaming is one row now. It used to be an edit of every member's bag, and
    # missing one silently broke the pairing.
    assert (
        client.get(f"/api/v1/products/{board['id']}").json()["data"]["family"]["name"]
        == "Cashmere RH"
    )


def test_deleting_a_family_unassigns_its_products_instead_of_deleting_them(client):
    """A family is a grouping; deleting a grouping must never delete catalog rows."""
    fam = _family(client, "Cashmere")
    board = _board(client, family_id=fam["id"])

    assert client.delete(f"/api/v1/product-families/{fam['id']}").status_code == 204

    survivor = client.get(f"/api/v1/products/{board['id']}").json()["data"]
    assert survivor["familyId"] is None
    assert survivor["family"] is None


def test_an_unknown_family_on_a_product_is_a_404_not_an_integrity_error(client):
    """The FK violation would otherwise surface through ``_conflict_detail`` as a
    generic 409 "Violación de restricción de integridad" — wrong status, and
    useless to whoever sent the id."""
    resp = client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": "X",
            "name": "X",
            "price": 1.0,
            "familyId": 9999,
            "attributes": {"height": 2800, "width": 2070, "thickness": 15},
        },
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Bulk assignment
# --------------------------------------------------------------------------- #
def test_bulk_assignment_moves_every_product(client):
    fam = _family(client, "Cashmere")
    ids = [_board(client, code=f"B{n}", name=f"Tablero {n}")["id"] for n in range(3)]
    resp = client.post(
        "/api/v1/product-families/assignments",
        json={"familyId": fam["id"], "productIds": ids},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["assigned"] == 3
    assert (
        client.get(f"/api/v1/product-families/{fam['id']}").json()["data"]["boardCount"]
        == 3
    )


def test_bulk_unassignment_is_the_same_endpoint_with_a_null_family(client):
    fam = _family(client, "Cashmere")
    board = _board(client, family_id=fam["id"])
    resp = client.post(
        "/api/v1/product-families/assignments",
        json={"familyId": None, "productIds": [board["id"]]},
    )
    assert resp.json()["data"]["assigned"] == 1
    assert (
        client.get(f"/api/v1/products/{board['id']}").json()["data"]["familyId"] is None
    )


def test_an_unknown_product_id_writes_nothing(client):
    """A silent partial write is the worst outcome on a bulk screen: the operator
    believes they moved 3 products and moved 2, with nothing saying which."""
    fam = _family(client, "Cashmere")
    good = _board(client, code="B1", name="Tablero 1")["id"]
    resp = client.post(
        "/api/v1/product-families/assignments",
        json={"familyId": fam["id"], "productIds": [good, 999999]},
    )
    assert resp.status_code == 404
    assert client.get(f"/api/v1/products/{good}").json()["data"]["familyId"] is None


def test_assignment_stamps_the_actor(client, db_session):
    """``CRUDService._stamp_actor`` only runs inside ``_persist``, so a
    statement-level bulk UPDATE would silently drop ``updated_by`` on the single
    largest edit anybody makes in this system."""
    from src.modules.products.model import ProductModel

    fam = _family(client, "Cashmere")
    board = _board(client, family_id=None)
    client.post(
        "/api/v1/product-families/assignments",
        json={"familyId": fam["id"], "productIds": [board["id"]]},
    )
    db_session.expire_all()
    row = db_session.get(ProductModel, board["id"])
    assert row.updated_by is not None


# --------------------------------------------------------------------------- #
# Fixing a missing alias from the screen that reports it
# --------------------------------------------------------------------------- #
def test_the_listing_counts_tapes_with_no_alias(client):
    """The signal. A tape with no code prints an indistinguishable notation, and
    a sync that brings a new width of a known design is exactly how one appears."""
    fam = _family(client, "Cashmere")
    _band(client, code="T19", family_id=fam["id"], width=19, alias="CSH")
    _band(client, code="T22", family_id=fam["id"], width=22, alias=None)

    (row,) = client.get("/api/v1/product-families/").json()["data"]
    assert row["missingAliasCount"] == 1
    assert row["aliases"] == ["CSH"]


def test_the_alias_can_be_stamped_on_several_tapes_at_once(client):
    """And the fix, from the same screen. Reporting a problem without offering
    the repair is how a diagnostic stops being read."""
    fam = _family(client, "Cashmere")
    a = _band(client, code="T19", family_id=fam["id"], width=19, alias=None)
    b = _band(client, code="T22", family_id=fam["id"], width=22, alias=None)

    resp = client.post(
        f"/api/v1/product-families/{fam['id']}/alias",
        json={"alias": "CSH", "productIds": [a["id"], b["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"updated": 2, "alias": "CSH"}

    detail = client.get(f"/api/v1/product-families/{fam['id']}").json()["data"]
    assert [t["alias"] for t in detail["edgeBandings"]] == ["CSH", "CSH"]
    assert detail["missingAliasCount"] == 0


def test_stamping_an_alias_refuses_a_product_from_another_family(client):
    """Scoped on purpose: the ids come from this family's own detail view, so
    anything else is a mistake worth refusing rather than obeying."""
    fam = _family(client, "Cashmere")
    other = _family(client, "Ibiza")
    outsider = _band(client, code="T22", family_id=other["id"], width=22, alias=None)

    resp = client.post(
        f"/api/v1/product-families/{fam['id']}/alias",
        json={"alias": "CSH", "productIds": [outsider["id"]]},
    )
    assert resp.status_code == 422
    assert "no pertenece" in resp.json()["errors"][0]["message"]
    # ...and nothing was written.
    assert (
        client.get(f"/api/v1/products/{outsider['id']}").json()["data"]["alias"] is None
    )


def test_stamping_an_alias_refuses_a_board(client):
    """An alias is an edge-banding field — the same rule the column's CHECK holds,
    stated here so the message can explain itself."""
    fam = _family(client, "Cashmere")
    board = _board(client, family_id=fam["id"])

    resp = client.post(
        f"/api/v1/product-families/{fam['id']}/alias",
        json={"alias": "CSH", "productIds": [board["id"]]},
    )
    assert resp.status_code == 422
    assert "no es un tapacanto" in resp.json()["errors"][0]["message"]


def test_stamping_an_alias_is_all_or_nothing(client):
    """Same reasoning as bulk assignment: a partial write leaves the operator
    believing they fixed five tapes when they fixed three."""
    fam = _family(client, "Cashmere")
    good = _band(client, code="T19", family_id=fam["id"], width=19, alias=None)
    board = _board(client, family_id=fam["id"])

    resp = client.post(
        f"/api/v1/product-families/{fam['id']}/alias",
        json={"alias": "CSH", "productIds": [good["id"], board["id"]]},
    )
    assert resp.status_code == 422
    assert client.get(f"/api/v1/products/{good['id']}").json()["data"]["alias"] is None


def test_the_seller_cannot_stamp_an_alias(client, db_session):
    from src.modules.users.schemas import UserCreate
    from src.modules.users.service import UserService
    from src.shared.security import create_access_token

    fam = _family(client, "Cashmere")
    tape = _band(client, code="T19", family_id=fam["id"], width=19, alias=None)
    seller = UserService(db_session).create(
        UserCreate(
            email="seller-alias@empresa.com",
            password="seller-password",
            roles=["vendedor"],
            full_name="Seller",
            branch_id=1,
        )
    )
    admin_auth = client.headers["Authorization"]
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(seller.id, seller.roles)}"
    )
    try:
        resp = client.post(
            f"/api/v1/product-families/{fam['id']}/alias",
            json={"alias": "CSH", "productIds": [tape["id"]]},
        )
        assert resp.status_code == 403
    finally:
        client.headers["Authorization"] = admin_auth


# --------------------------------------------------------------------------- #
# The coverage report that replaced two catalog-sync warnings
# --------------------------------------------------------------------------- #
def test_the_listing_reports_counts_and_coverage(client):
    fam = _family(client, "Cashmere")
    _board(client, code="B36", family_id=fam["id"], thickness=36)
    _band(client, code="T19", family_id=fam["id"], width=19)

    (row,) = client.get("/api/v1/product-families/").json()["data"]
    assert (row["boardCount"], row["edgeBandingCount"]) == (1, 1)
    # A 36mm board whose design only comes in 19mm tape: the picker is as empty
    # as a broken family, and from the seller's chair it is the same problem.
    assert row["uncoveredThicknesses"] == [36.0]
    assert row["aliases"] == ["CSH"]


def test_issues_only_hides_the_healthy_families(client):
    healthy = _family(client, "Cashmere")
    _board(client, code="B15", family_id=healthy["id"], thickness=15)
    _band(client, code="T19", family_id=healthy["id"], width=19)
    lonely = _family(client, "Ibiza")
    _band(client, code="T22", family_id=lonely["id"], width=22, alias="IBZ")

    flagged = client.get(
        "/api/v1/product-families/", params={"issuesOnly": True}
    ).json()["data"]
    assert [f["name"] for f in flagged] == ["Ibiza"]
    assert flagged[0]["hasNoBoards"] is True


# --------------------------------------------------------------------------- #
# Filters on the product listing (the assignment queue)
# --------------------------------------------------------------------------- #
def test_products_can_be_listed_by_family_and_by_having_none(client):
    fam = _family(client, "Cashmere")
    assigned = _board(client, code="B1", name="Tablero 1", family_id=fam["id"])
    orphan = _board(client, code="B2", name="Tablero 2")

    by_family = client.get("/api/v1/products/", params={"familyId": fam["id"]}).json()
    assert [p["id"] for p in by_family["data"]] == [assigned["id"]]

    unassigned = client.get("/api/v1/products/", params={"unassigned": True}).json()
    assert [p["id"] for p in unassigned["data"]] == [orphan["id"]]


def test_the_assignment_queue_combines_with_the_type_and_subtype_filters(client):
    """What the bulk-assignment modal asks for, in one request.

    The modal counts each type under the SAME filters so its tabs say how many a
    click would really show — which is what stops it from looking board-only on a
    catalog where every tapacanto already has a design (76 unassigned products
    today, all of them boards). It also caps the page, so the totals have to be
    right or the footer hides the tail in silence.
    """
    fam = _family(client, "Cashmere")
    _board(client, code="B1", name="MDF fondo", family_id=None)
    _board(client, code="B2", name="Plywood SM", family_id=None)
    _band(client, code="T1", name="Tapacanto libre", family_id=None, alias=None)
    _band(client, code="T2", name="Tapacanto Cashmere", family_id=fam["id"])

    def total(**params):
        body = client.get("/api/v1/products/", params=params).json()
        return body["meta"]["pagination"]["total"]

    # The two tab counts, scoped to the queue.
    assert total(unassigned=True, type="board") == 2
    assert total(unassigned=True, type="edge_banding") == 1
    # ...and with the scope toggle lifted, the whole catalog.
    assert total(type="edge_banding") == 2

    # Type and subtype narrow together, and the filters AND across fields.
    assert total(unassigned=True, type="board", search="Plywood") == 1
    assert total(unassigned=True, type="board", familyId=fam["id"]) == 0


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def test_the_seller_reads_families_but_cannot_change_them(client, db_session):
    """Same matrix as the catalog itself: a family IS catalog data. Moving a board
    between designs changes what gets quoted, so it stays with the admin."""
    from src.modules.users.schemas import UserCreate
    from src.modules.users.service import UserService
    from src.shared.security import create_access_token

    fam = _family(client, "Cashmere")
    seller = UserService(db_session).create(
        UserCreate(
            email="seller-families@empresa.com",
            password="seller-password",
            roles=["vendedor"],
            full_name="Seller",
            branch_id=1,
        )
    )
    admin_auth = client.headers["Authorization"]
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(seller.id, seller.roles)}"
    )
    try:
        assert client.get("/api/v1/product-families/").status_code == 200
        assert client.get(f"/api/v1/product-families/{fam['id']}").status_code == 200
        assert (
            client.post("/api/v1/product-families/", json={"name": "Ibiza"}).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/v1/product-families/{fam['id']}", json={"name": "X"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/product-families/assignments",
                json={"familyId": fam["id"], "productIds": [1]},
            ).status_code
            == 403
        )
        assert client.delete(f"/api/v1/product-families/{fam['id']}").status_code == 403
    finally:
        client.headers["Authorization"] = admin_auth
