"""Tests for the client review flow on pre-orders (secure link).

The link and confirmation live on the pre-order (mutable); confirming mints the
immutable Order and links it. Covers the link cryptography, the token-gated
public endpoints, and idempotent order creation.
"""

from datetime import datetime, timedelta

from src.modules.orders.model import OrderModel
from src.modules.preorders.model import PreOrderModel
from src.shared.config import config

from .test_edge_banding import _create_edge_banding
from .test_orders import _create_board, _create_client, _order_payload


def _create_preorder(client, c, b, **kwargs):
    return client.post(
        "/api/v1/preorders/", json=_order_payload(c["id"], b["id"], **kwargs)
    ).json()["data"]


def _setup_preorder(client, **kwargs):
    c = _create_client(client)
    b = _create_board(client)
    return _create_preorder(client, c, b, **kwargs)


def _generate_link(client, preorder_id):
    resp = client.post(f"/api/v1/preorders/{preorder_id}/review-link")
    assert resp.status_code == 201
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# Review link generation
# ---------------------------------------------------------------------------


def test_generate_link_marks_sent_and_returns_token(client, monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_BASE_URL", "https://maderable.ec")
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    assert link["status"] == "active"
    assert len(link["token"]) >= 40
    assert link["url"] == f"https://maderable.ec/review/{link['token']}"
    assert link["expiresAt"] is not None

    fetched = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    assert fetched["status"] == "sent"
    assert fetched["sentAt"] is not None

    info = client.get(f"/api/v1/preorders/{pre['id']}/review-link")
    assert info.status_code == 200
    body = info.json()["data"]
    assert body["status"] == "active"
    assert "token" not in body
    assert link["token"] not in info.text


def test_generate_link_requires_phone(client):
    c = client.post(
        "/api/v1/clients/",
        json={"identifier": "sin-celular", "firstName": "Sin", "lastName": "Celular"},
    ).json()["data"]
    b = _create_board(client)
    pre = client.post(
        "/api/v1/preorders/", json=_order_payload(c["id"], b["id"])
    ).json()["data"]

    resp = client.post(f"/api/v1/preorders/{pre['id']}/review-link")
    assert resp.status_code == 422
    assert "celular" in resp.json()["errors"][0]["message"]


def test_generate_link_requires_open_status(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/confirm")

    resp = client.post(f"/api/v1/preorders/{pre['id']}/review-link")
    assert resp.status_code == 422
    assert "abierta" in resp.json()["errors"][0]["message"]


def test_link_endpoints_404(client):
    assert client.post("/api/v1/preorders/999999/review-link").status_code == 404
    assert client.get("/api/v1/preorders/999999/review-link").status_code == 404
    pre = _setup_preorder(client)
    assert client.get(f"/api/v1/preorders/{pre['id']}/review-link").status_code == 404


def test_regenerate_revokes_previous_link(client):
    pre = _setup_preorder(client)
    first = _generate_link(client, pre["id"])
    second = _generate_link(client, pre["id"])

    assert client.get(f"/api/v1/public/review/{first['token']}").status_code == 404
    assert client.get(f"/api/v1/public/review/{second['token']}").status_code == 200


# ---------------------------------------------------------------------------
# Public endpoints (token as credential)
# ---------------------------------------------------------------------------


def test_public_review_detail_is_sanitized(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    resp = client.get(f"/api/v1/public/review/{link['token']}")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["reference"] == pre["code"]
    assert data["status"] == "sent"
    assert data["orderCode"] is None
    assert data["clientName"] == "Ada Lovelace"
    assert data["subtotal"] == pre["optimization"]["totalBoardsCost"]
    assert data["taxAmount"] == round(data["subtotal"] * 0.15, 2)
    assert data["total"] == round(data["subtotal"] + data["taxAmount"], 2)
    assert len(data["lines"]) == 1
    assert data["lines"][0]["productCode"] == "MEL18"
    assert len(data["pieces"]) == 1
    assert data["pieces"][0]["height"] == 800

    # Sanitization: no contact info, internal identifiers, or raw inputs.
    assert "0100000397" not in resp.text
    for leaked in ("identifier", "phone", "email", "clientId", "materials"):
        assert leaked not in data


def test_public_review_exposes_the_cutting_layout(client):
    """The client sees how their boards get cut, not just what they're billed."""
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    data = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]

    groups = data["layoutGroups"]
    assert groups, "the diagram needs at least one cutting pattern"
    # Grouping dedupes by pattern, so the sheet count is the sum of the groups'.
    assert sum(g["count"] for g in groups) == data["totalBoardsUsed"]
    assert data["totalPieces"] == sum(p["quantity"] for p in data["pieces"])

    group = groups[0]
    assert group["sheet"]["materialName"]
    assert group["sheet"]["width"] > 0 and group["sheet"]["height"] > 0
    assert len(group["placedPieces"]) == group["piecesCount"]

    piece = group["placedPieces"][0]
    # ``pieceId`` is the label the client typed, so the diagram can name pieces.
    assert piece["pieceId"]
    for key in ("x", "y", "width", "height", "originalWidth", "originalHeight"):
        assert isinstance(piece[key], (int, float))


def test_public_review_layout_carries_both_edge_frames(client):
    """A rotated piece ships its bands in both frames, and the notation stays nominal.

    ``sides`` is rotated so the diagram paints the band on the right physical
    edge; ``nominalSides`` and ``notation`` describe the piece as the client
    ordered it. Deriving one from the other client-side is what turned a ``1L``
    into a ``1C``.
    """
    c = _create_client(client)
    b = _create_board(client)  # 2440 (height) × 1220 (width)
    eb = _create_edge_banding(client, band_type="Soft")

    # 1000 (height) × 2000 (width): only fits rotated, since 2000 > the board's 1220.
    pre = client.post(
        "/api/v1/preorders/",
        json={
            "clientId": c["id"],
            "branchId": 1,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [
                {
                    "priority": 0,
                    "height": 1000,
                    "width": 2000,
                    "quantity": 1,
                    "materialKey": "b1",
                    "label": "Costado",
                    "canRotate": True,
                    "edgeBanding": {"productId": eb["id"], "sides": ["left"]},
                }
            ],
        },
    ).json()["data"]
    link = _generate_link(client, pre["id"])

    data = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]
    piece = data["layoutGroups"][0]["placedPieces"][0]

    assert piece["rotated"] is True
    edges = piece["edges"]
    assert edges["sides"] == ["top"]  # CW: left→top
    assert edges["nominalSides"] == ["left"]
    # left = a height side = long ⇒ 1L, and Soft ⇒ CS. Unchanged by the rotation.
    assert edges["notation"] == "1L CS"


def test_public_review_layout_hides_production_internals(client):
    """The geometry is projected, not forwarded: no catalog ids, no saw paths."""
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    resp = client.get(f"/api/v1/public/review/{link['token']}")
    group = resp.json()["data"]["layoutGroups"][0]

    # ``materialKey`` is the seller's internal handle; ``cuts`` is saw travel.
    assert "materialKey" not in group
    assert "materialKey" not in group["sheet"]
    for leaked in ("cuts", "statistics", "patternId"):
        assert leaked not in group
    for piece in group["placedPieces"]:
        edges = piece.get("edges")
        if edges:
            for leaked in ("productId", "code", "product_id"):
                assert leaked not in edges


def test_public_review_shows_the_commercial_reference(client):
    """The client sees the same reference that's printed on the proforma PDF."""
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Proyecto Casa Pérez — cocina"
    pre = client.post("/api/v1/preorders/", json=payload).json()["data"]
    link = _generate_link(client, pre["id"])

    data = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]
    assert data["notes"] == "Proyecto Casa Pérez — cocina"


def test_public_review_unknown_token_404(client):
    resp = client.get("/api/v1/public/review/un-token-que-no-existe")
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["message"] == "Enlace de revisión no válido"


def test_confirm_creates_immutable_order(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    resp = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "confirmed"
    assert data["orderCode"] and data["orderCode"].startswith("ORD-")

    pre_after = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    assert pre_after["status"] == "confirmed"
    assert pre_after["confirmedAt"] is not None
    assert pre_after["orderId"] is not None

    order = client.get(f"/api/v1/orders/{pre_after['orderId']}").json()["data"]
    assert order["status"] == "confirmed"
    assert order["code"] == data["orderCode"]

    info = client.get(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    assert info["status"] == "used"
    assert info["usedAt"] is not None


def test_confirm_inherits_preorder_strategy(client, db_session):
    """On confirmation, the order inherits and freezes the pre-order's strategy."""
    pre = _setup_preorder(client, strategy="longOffcuts")
    assert pre["strategy"] == "longOffcuts"
    link = _generate_link(client, pre["id"])

    resp = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert resp.status_code == 200

    order_id = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]["orderId"]
    db_session.expire_all()
    order = db_session.get(OrderModel, order_id)
    assert order.optimization_snapshot["strategy"] == "longOffcuts"


def test_reconfirm_is_benign_and_does_not_duplicate_order(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    first = client.post(f"/api/v1/public/review/{link['token']}/confirm").json()["data"]

    again = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert again.status_code == 200
    assert again.json()["data"]["orderCode"] == first["orderCode"]

    # No second order was created for the client.
    orders = client.get("/api/v1/orders/").json()
    assert orders["meta"]["pagination"]["total"] == 1


def test_reject_via_token(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    resp = client.post(
        f"/api/v1/public/review/{link['token']}/reject", json={"note": "Muy caro"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "rejected"

    # Repeated rejection: benign.
    assert (
        client.post(f"/api/v1/public/review/{link['token']}/reject").status_code == 200
    )


def test_reject_after_confirm_conflicts(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/confirm")

    resp = client.post(f"/api/v1/public/review/{link['token']}/reject")
    assert resp.status_code == 409
    assert "ventas" in resp.json()["errors"][0]["message"]


# ---------------------------------------------------------------------------
# Request changes (edit loop without discarding the quote)
# ---------------------------------------------------------------------------


def test_request_changes_keeps_link_alive_and_records_note(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    resp = client.post(
        f"/api/v1/public/review/{link['token']}/request-changes",
        json={"note": "Cámbiame el alto a 450"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "changes_requested"
    assert data["clientNote"] == "Cámbiame el alto a 450"

    # The link stays alive: the client can reopen it.
    again = client.get(f"/api/v1/public/review/{link['token']}")
    assert again.status_code == 200
    assert again.json()["data"]["status"] == "changes_requested"

    # The shop sees the request in the internal detail.
    detail = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    assert detail["status"] == "changes_requested"
    assert detail["clientNote"] == "Cámbiame el alto a 450"


def test_shop_edit_after_request_returns_to_sent_and_clears_note(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(
        f"/api/v1/public/review/{link['token']}/request-changes",
        json={"note": "Otra medida"},
    )

    edited = client.put(
        f"/api/v1/preorders/{pre['id']}",
        json={
            "requirements": [
                {
                    "priority": 0,
                    "height": 450,
                    "width": 600,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Puerta",
                    "canRotate": True,
                }
            ]
        },
    )
    assert edited.status_code == 200
    data = edited.json()["data"]
    assert data["status"] == "sent"
    assert data["clientNote"] is None

    # The same link shows the edited version (live prices/measurements).
    review = client.get(f"/api/v1/public/review/{link['token']}").json()["data"]
    assert review["status"] == "sent"
    assert review["pieces"][0]["height"] == 450


def test_confirm_after_request_changes(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/request-changes")

    resp = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "confirmed"
    assert resp.json()["data"]["orderCode"].startswith("ORD-")


def test_request_changes_after_confirm_conflicts(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/confirm")

    resp = client.post(f"/api/v1/public/review/{link['token']}/request-changes")
    assert resp.status_code == 409
    assert "ventas" in resp.json()["errors"][0]["message"]


def test_confirm_expired_quote_fails(client, db_session):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    db_pre = db_session.get(PreOrderModel, pre["id"])
    db_pre.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    resp = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert resp.status_code == 422
    assert "expiró" in resp.json()["errors"][0]["message"]

    # GET with a valid token on an expired quote is NOT a 404: friendly page.
    detail = client.get(f"/api/v1/public/review/{link['token']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "expired"


def test_public_proforma_endpoint_removed(client):
    """The public review link no longer serves a proforma PDF (removed with the
    frontend download button); even a valid token 404s on that path."""
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])

    assert (
        client.get(f"/api/v1/public/review/{link['token']}/proforma").status_code == 404
    )


def test_confirmed_order_continues_state_machine(client):
    """After client confirmation, the order continues the normal operational flow."""
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/confirm")
    pre_after = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]

    ok = client.patch(
        f"/api/v1/orders/{pre_after['orderId']}/status",
        json={"status": "queued", "payment": {"cashAmount": 100.0}},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "queued"
