"""Tests for the preorders module: mutable CRUD, recompute, cap, and expiration."""

from datetime import datetime, timedelta

from src.modules.preorders.model import PreOrderModel

from .test_orders import _BRANCH, _create_board, _create_client, _order_payload


def _setup(client):
    return _create_client(client), _create_board(client)


def _create_preorder(client, c, b, **kwargs):
    return client.post(
        "/api/v1/preorders/", json=_order_payload(c["id"], b["id"], **kwargs)
    )


def test_create_preorder_is_draft_with_live_optimization(client):
    c, b = _setup(client)
    resp = _create_preorder(client, c, b)
    assert resp.status_code == 201
    data = resp.json()["data"]

    assert data["status"] == "draft"
    assert data["code"].startswith("PRE-")
    assert data["orderId"] is None
    assert data["client"]["id"] == c["id"]
    # The pre-order exposes its owning branch (compact reference).
    assert data["branch"]["id"] == 1
    assert data["branch"]["code"] == "MATRIZ"
    assert data["expiresAt"] is not None

    # Raw editable inputs (what the optimizer form re-renders).
    assert len(data["materials"]) == 1
    assert data["materials"][0]["key"] == "b1"
    assert data["materials"][0]["source"] == "catalog"
    assert data["materials"][0]["productId"] == b["id"]
    assert len(data["requirements"]) == 1
    assert data["requirements"][0]["materialKey"] == "b1"
    assert data["requirements"][0]["height"] == 800

    # Embedded recomputed optimization (live prices, nothing frozen).
    opt = data["optimization"]
    assert opt["totalBoardsUsed"] >= 1
    assert len(opt["materialsSummary"]) == 1
    assert opt["materialsSummary"][0]["productCode"] == "MEL18"


def test_update_preorder_recomputes_totals(client):
    c, b = _setup(client)
    pre = _create_preorder(client, c, b, quantity=2).json()["data"]
    boards_before = pre["optimization"]["totalBoardsUsed"]

    upd = client.put(
        f"/api/v1/preorders/{pre['id']}",
        json={
            "requirements": [
                {
                    "priority": 0,
                    "height": 400,
                    "width": 600,
                    "quantity": 40,
                    "materialKey": "b1",
                    "label": "Puerta",
                    "canRotate": True,
                }
            ]
        },
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["optimization"]["totalBoardsUsed"] > boards_before


def test_update_blocked_when_not_open(client, db_session):
    c, b = _setup(client)
    pre = _create_preorder(client, c, b).json()["data"]

    # Expire the pre-order: reading it marks it 'expired' and it can no longer be edited.
    db_pre = db_session.get(PreOrderModel, pre["id"])
    db_pre.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    resp = client.put(f"/api/v1/preorders/{pre['id']}", json={"notes": "x"})
    assert resp.status_code == 422
    assert "ya no puede editarse" in resp.json()["errors"][0]["message"]


def test_open_cap_enforced(client):
    # The cap is read from settings (not env): lower it to 2 via the settings API.
    patched = client.patch(
        "/api/v1/settings/preorders", json={"maxOpenPreordersPerClient": 2}
    )
    assert patched.status_code == 200
    c, b = _setup(client)
    assert _create_preorder(client, c, b, width=600).status_code == 201
    assert _create_preorder(client, c, b, width=500).status_code == 201

    blocked = _create_preorder(client, c, b, width=400)
    assert blocked.status_code == 422
    assert "abierta" in blocked.json()["errors"][0]["message"]


def test_list_filter_and_summary_omits_optimization(client):
    c, b = _setup(client)
    _create_preorder(client, c, b)
    listed = client.get("/api/v1/preorders/?status=draft").json()
    assert listed["meta"]["pagination"]["total"] >= 1
    assert all(item["status"] == "draft" for item in listed["data"])
    # The lightweight summary doesn't include the full optimization.
    assert "optimization" not in listed["data"][0]


def test_list_exposes_notes_as_the_listing_reference(client):
    """The listing carries ``notes`` so the dashboard can show it as a subtitle
    under the code (the differentiator between quotes of the same client)."""
    c, b = _setup(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Proyecto Casa Pérez — cocina"
    client.post("/api/v1/preorders/", json=payload)

    item = client.get("/api/v1/preorders/?status=draft").json()["data"][0]
    assert item["notes"] == "Proyecto Casa Pérez — cocina"


def test_delete_preorder(client):
    c, b = _setup(client)
    pre = _create_preorder(client, c, b).json()["data"]
    assert client.delete(f"/api/v1/preorders/{pre['id']}").status_code == 204
    assert client.get(f"/api/v1/preorders/{pre['id']}").status_code == 404


def test_create_preorder_unknown_client_404(client):
    _, b = _setup(client)
    resp = client.post("/api/v1/preorders/", json=_order_payload(999999, b["id"]))
    assert resp.status_code == 404


def test_preorder_proforma_pdf(client):
    c, b = _setup(client)
    pre = _create_preorder(client, c, b).json()["data"]
    pdf = client.get(f"/api/v1/preorders/{pre['id']}/proforma")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 1000


def test_preorder_proforma_prints_the_reference(client):
    """``notes`` reaches the proforma as the "Ref:" line next to the N°/date."""
    import io

    from pypdf import PdfReader

    c, b = _setup(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Proyecto Alfa"
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]

    pdf = client.get(f"/api/v1/preorders/{pre['id']}/proforma")
    assert pdf.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
    )
    assert "Ref: Proyecto Alfa" in text


def test_preorder_proforma_pdf_inline_material_without_label(client):
    """Regression: an inline (manual/offcut) material leaves product_code/product_name None; the
    proforma must render "N/A" instead of crashing on Paragraph(None) (was a 500)."""
    c = _create_client(client)
    payload = {
        "clientId": c["id"],
        "branchId": _BRANCH,
        "materials": [
            {
                "key": "m1",
                "source": "manual",
                "height": 2000,
                "width": 1000,
                "thickness": 18,
                "costPerUnit": 30.0,
                # label omitted → product_name/product_code resolve to None
            }
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 2,
                "materialKey": "m1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }
    pre = client.post("/api/v1/preorders/", json=payload)
    assert pre.status_code == 201, pre.text
    pdf = client.get(
        f"/api/v1/preorders/{pre.json()['data']['id']}/proforma?format=pdf"
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 1000


def test_preorder_persists_strategy_and_recomputes_with_it(client):
    """The strategy is saved and the recompute (cache-first) uses it on every read."""
    c, b = _setup(client)
    data = _create_preorder(client, c, b, strategy="longOffcuts").json()["data"]
    assert data["strategy"] == "longOffcuts"
    assert data["optimization"]["strategy"] == "longOffcuts"

    # Re-reading the pre-order recomputes again and keeps the strategy.
    reread = client.get(f"/api/v1/preorders/{data['id']}").json()["data"]
    assert reread["strategy"] == "longOffcuts"
    assert reread["optimization"]["strategy"] == "longOffcuts"

    # Omitting the strategy falls back to the default behavior.
    other = _create_preorder(client, c, b, width=500).json()["data"]
    assert other["strategy"] == "default"
    assert other["optimization"]["strategy"] == "default"


def test_update_preorder_changes_strategy(client):
    c, b = _setup(client)
    pre = _create_preorder(client, c, b).json()["data"]
    assert pre["strategy"] == "default"

    upd = client.put(f"/api/v1/preorders/{pre['id']}", json={"strategy": "longOffcuts"})
    assert upd.status_code == 200
    data = upd.json()["data"]
    assert data["strategy"] == "longOffcuts"
    assert data["optimization"]["strategy"] == "longOffcuts"


def test_list_preorders_filter_by_multiple_statuses(client, db_session):
    """Repeating ``status`` filters by several at once; one occurrence still works."""
    c, b = _setup(client)
    p1 = _create_preorder(client, c, b, width=600).json()["data"]
    p2 = _create_preorder(client, c, b, width=500).json()["data"]
    p3 = _create_preorder(client, c, b, width=400).json()["data"]

    # Move two of them off 'draft' directly: the transitions themselves are
    # covered by the review-flow tests, this one is about the filter.
    db_session.get(PreOrderModel, p2["id"]).status = "sent"
    db_session.get(PreOrderModel, p3["id"]).status = "confirmed"
    db_session.commit()

    both = client.get(
        "/api/v1/preorders/", params={"status": ["draft", "confirmed"]}
    ).json()
    assert {p["id"] for p in both["data"]} == {p1["id"], p3["id"]}
    assert both["meta"]["pagination"]["total"] == 2

    # A single occurrence keeps behaving as it did before the param went plural.
    only_sent = client.get("/api/v1/preorders/", params={"status": "sent"}).json()
    assert [p["id"] for p in only_sent["data"]] == [p2["id"]]


def test_list_preorders_defaults_to_newest_first(client):
    """Nothing reads this listing FIFO, and newest-first is what it always returned."""
    c, b = _setup(client)
    p1 = _create_preorder(client, c, b, width=600).json()["data"]
    p2 = _create_preorder(client, c, b, width=500).json()["data"]
    p3 = _create_preorder(client, c, b, width=400).json()["data"]

    resp = client.get("/api/v1/preorders/").json()
    assert [p["id"] for p in resp["data"]] == [p3["id"], p2["id"], p1["id"]]

    oldest = client.get("/api/v1/preorders/", params={"sort": "oldest"}).json()
    assert [p["id"] for p in oldest["data"]] == [p1["id"], p2["id"], p3["id"]]


def test_list_preorders_search_by_code_id_and_client(client):
    c1 = _create_client(client)
    c2 = _create_client(client, identifier="0100000017", phone="0100000017")
    # Distinguish the second client: _create_client always names them Ada Lovelace.
    client.put(
        f"/api/v1/clients/{c2['id']}",
        json={
            "identifier": "0100000017",
            "firstName": "Grace",
            "lastName": "Hopper",
            "phone": "0100000017",
        },
    )
    b = _create_board(client)
    p1 = _create_preorder(client, c1, b, width=600).json()["data"]
    p2 = _create_preorder(client, c2, b, width=500).json()["data"]

    # By quote code (a fragment of it is enough).
    by_code = client.get("/api/v1/preorders/", params={"search": p2["code"]}).json()
    assert [p["id"] for p in by_code["data"]] == [p2["id"]]

    # By client last name, case-insensitive.
    by_client = client.get("/api/v1/preorders/", params={"search": "hopper"}).json()
    assert [p["id"] for p in by_client["data"]] == [p2["id"]]

    # By client identifier.
    by_ident = client.get("/api/v1/preorders/", params={"search": "0100000017"}).json()
    assert [p["id"] for p in by_ident["data"]] == [p2["id"]]

    # An all-digit term also matches the pre-order id exactly.
    by_id = client.get("/api/v1/preorders/", params={"search": str(p1["id"])}).json()
    assert p1["id"] in [p["id"] for p in by_id["data"]]

    # No match: empty page, and the total reflects the filter, not the table.
    none = client.get("/api/v1/preorders/", params={"search": "zzzz"}).json()
    assert none["data"] == []
    assert none["meta"]["pagination"]["total"] == 0


def test_list_preorders_filter_by_client(client):
    c1 = _create_client(client)
    c2 = _create_client(client, identifier="0100000017", phone="0100000017")
    b = _create_board(client)
    p1 = _create_preorder(client, c1, b, width=600).json()["data"]
    p2 = _create_preorder(client, c2, b, width=500).json()["data"]

    resp = client.get("/api/v1/preorders/", params={"clientId": c2["id"]}).json()
    assert [p["id"] for p in resp["data"]] == [p2["id"]]
    assert p1["id"] not in [p["id"] for p in resp["data"]]
    assert resp["meta"]["pagination"]["total"] == 1


def test_list_preorders_filter_by_created_day_range(client, db_session):
    c, b = _setup(client)
    p1 = _create_preorder(client, c, b, width=600).json()["data"]
    p2 = _create_preorder(client, c, b, width=500).json()["data"]

    # Backdate the first one; created_at is UTC-naive, so the range is a UTC day.
    db_session.query(PreOrderModel).filter(PreOrderModel.id == p1["id"]).update(
        {"created_at": datetime.utcnow() - timedelta(days=3)}
    )
    db_session.commit()

    today = datetime.utcnow().date()
    old_day = today - timedelta(days=3)

    # `createdTo` is inclusive: the backdated quote's own day must return it.
    upto = client.get(
        "/api/v1/preorders/", params={"createdTo": old_day.isoformat()}
    ).json()
    assert [p["id"] for p in upto["data"]] == [p1["id"]]

    # `createdFrom` is inclusive too, and today's quote is on today's boundary.
    since = client.get(
        "/api/v1/preorders/", params={"createdFrom": today.isoformat()}
    ).json()
    assert [p["id"] for p in since["data"]] == [p2["id"]]


def test_preorder_carries_whole_board_through_the_recompute(client):
    """The flag rides in the stored ``materials`` JSON, with no column of its own.

    A pre-order re-optimizes on every read, so a commercial flag that lives
    outside the hash only survives if it round-trips through ``build_request``.
    """
    c, b = _setup(client)
    payload = _order_payload(c["id"], b["id"], height=300, width=300, quantity=1)
    payload["materials"][0]["wholeBoard"] = True
    created = client.post("/api/v1/preorders/", json=payload).json()["data"]

    assert created["materials"][0]["wholeBoard"] is True
    line = created["optimization"]["materialsSummary"][0]
    assert line["halfBoard"] is False
    assert line["costPerUnit"] == 45.5

    # Re-read: the recompute (cache hit) has to promote it again.
    fetched = client.get(f"/api/v1/preorders/{created['id']}").json()["data"]
    sheet = fetched["optimization"]["layouts"][0]["material"]
    assert sheet["halfBoard"] is False
    assert sheet["width"] == 1220
    assert fetched["optimization"]["totalBoardsCost"] == 45.5


# --- Material graph: rejected at write time, not on the next read -------------


def test_create_preorder_rejects_an_orphan_requirement(client):
    """An inconsistent set used to be SAVED and only blow up when read.

    ``build_request`` re-validates on every read, and a raw Pydantic error there
    is not a ``RequestValidationError`` — it surfaced as a 500 on the detail, the
    PDF and the confirm, i.e. a quote that could no longer be opened at all.
    """
    c, b = _setup(client)
    payload = _order_payload(c["id"], b["id"])
    payload["requirements"][0]["materialKey"] = "does-not-exist"

    resp = client.post("/api/v1/preorders/", json=payload)
    assert resp.status_code == 422


def test_update_preorder_rejects_orphaning_a_stored_requirement(client):
    """Checked on the MERGED pair: this update only sends materials."""
    c, b = _setup(client)
    pre = _create_preorder(client, c, b).json()["data"]

    resp = client.put(
        f"/api/v1/preorders/{pre['id']}",
        json={
            "materials": [
                {"key": "otro", "source": "catalog", "productId": b["id"]},
            ]
        },
    )
    assert resp.status_code == 422

    # The stored pre-order is untouched and still readable.
    assert client.get(f"/api/v1/preorders/{pre['id']}").status_code == 200


def test_preorder_on_client_offcuts_only_round_trips(client):
    """The retazo-anchored shape survives store → re-optimize → read."""
    c = _create_client(client)
    payload = {
        "clientId": c["id"],
        "branchId": _BRANCH,
        "materials": [
            {
                "key": "r1",
                "source": "clientOffcut",
                "height": 1000,
                "width": 1000,
                "thickness": 18,
                "label": "Retazo grande",
            },
            {
                "key": "r2",
                "source": "clientOffcut",
                "height": 1000,
                "width": 1000,
                "thickness": 18,
                "poolKey": "r1",
                "label": "Retazo chico",
            },
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 900,
                "width": 900,
                "quantity": 3,
                "materialKey": "r1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }

    created = client.post("/api/v1/preorders/", json=payload)
    assert created.status_code == 201
    data = created.json()["data"]

    # Both retazos are stored, the pooled one keeping its link.
    assert [m["key"] for m in data["materials"]] == ["r1", "r2"]
    assert data["materials"][1]["poolKey"] == "r1"

    opt = data["optimization"]
    assert opt["totalBoardsUsed"] == 0
    # Two retazos hold one 900×900 each; the third piece is reported, not dropped.
    assert len(opt["layouts"]) == 2
    assert opt["unplaced"][0]["quantity"] == 1

    # The proforma renders over the same snapshot.
    assert client.get(f"/api/v1/preorders/{data['id']}/proforma").status_code == 200
