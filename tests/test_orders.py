"""Tests for the orders module: creation (snapshot), idempotency, statuses.

Orders are no longer created via HTTP (``POST /orders`` was removed): they're born
when a pre-order is confirmed. Here they're minted directly via ``OrderService.create``
(the internal path that preserves the flow) reusing the ``client`` fixture's session,
and read back via GET to verify the camelCase API projection.
"""

from datetime import datetime, timedelta

import pytest

from src.modules.orders.model import OrderModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError


def _create_client(client, identifier="0100000397", phone="0100000397"):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": identifier,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": phone,
        },
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


# Default branch seeded by conftest (id=1).
_BRANCH = 1


def _order_payload(
    client_id, product_id, height=800, width=700, quantity=2, strategy=None
):
    # Default non-halvable (both sides > half-width of 610): full-board charging is
    # tested with a job that doesn't fit in half a board. Half-board tests pass
    # explicit small dimensions.
    payload = {
        "clientId": client_id,
        "branchId": _BRANCH,
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": height,
                "width": width,
                "quantity": quantity,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }
    if strategy is not None:
        payload["strategy"] = strategy
    return payload


def _mint_order(db_session, payload):
    """Creates the order via the service and returns the ``OrderModel``."""
    return OrderService(db_session).create(OrderCreate.model_validate(payload))


def _create_order(client, db_session, payload):
    """Mints the order and returns its camelCase API projection (GET)."""
    order = _mint_order(db_session, payload)
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def test_create_order_freezes_snapshot_and_charges_boards(client, db_session):
    c = _create_client(client)
    b = _create_board(client)

    data = _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    assert data["status"] == "confirmed"
    assert data["code"] == f"ORD-{datetime.utcnow().year}-{data['id']:04d}"
    assert data["client"]["id"] == c["id"]
    # The order exposes its owning branch (compact reference) for the dashboard.
    assert data["branch"]["id"] == _BRANCH
    assert data["branch"]["code"] == "MATRIZ"
    assert data["branch"]["name"] == "Casa Matriz"
    assert len(data["optimizationHash"]) == 64

    # Charge = boards: one line per board type (from materials_summary).
    assert len(data["lines"]) == 1
    line = data["lines"][0]
    assert line["productCode"] == "MEL18"
    assert line["quantity"] == data["totalBoardsUsed"]
    assert line["lineTotal"] == line["quantity"] * 45.5

    # Immutable totals: the net subtotal is the sum across boards, the total
    # adds the tax frozen with the order.
    assert data["subtotal"] == line["lineTotal"]
    assert data["taxRate"] == 0.15
    # The tax is rounded to cents and THEN added, as an invoice does — not
    # `subtotal * 1.15` in one step, which lands a cent lower here.
    assert data["taxAmount"] == round(data["subtotal"] * 0.15, 2)
    assert data["total"] == round(data["subtotal"] + data["taxAmount"], 2)

    # The order no longer carries validity (the mutable quote lives in the pre-order).
    assert "expiresAt" not in data

    # Cutting list = pieces (production input, not charged).
    assert len(data["pieces"]) == 1
    piece = data["pieces"][0]
    assert piece["height"] == 800 and piece["width"] == 700
    assert piece["quantity"] == 2

    # Initial history records the creation.
    assert data["history"][0]["toStatus"] == "confirmed"
    assert data["history"][0]["fromStatus"] is None


def test_create_order_blocked_without_client_phone(client, db_session):
    """Business rule: without a registered phone, the order isn't created (422)."""
    b = _create_board(client)
    no_phone = client.post(
        "/api/v1/clients/",
        json={"identifier": "0100000025", "firstName": "Sin", "lastName": "Tel"},
    ).json()["data"]

    with pytest.raises(BusinessRuleError) as exc:
        _mint_order(db_session, _order_payload(no_phone["id"], b["id"]))
    assert "celular" in str(exc.value).lower()
    # No order was persisted.
    assert client.get("/api/v1/orders/").json()["data"] == []


def test_create_order_unknown_client_returns_404(client, db_session):
    """A nonexistent ``clientId`` gives a clean 404 before freezing anything."""
    b = _create_board(client)
    with pytest.raises(EntityNotFoundError) as exc:
        _mint_order(db_session, _order_payload(99999, b["id"]))
    assert "Client 99999" in str(exc.value)


def test_create_order_is_idempotent(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])

    first = _mint_order(db_session, payload)
    second = _mint_order(db_session, payload)

    assert first.id == second.id
    assert first.code == second.code
    # Two orders aren't created for the same (client, hash) pair.
    assert len(client.get("/api/v1/orders/").json()["data"]) == 1


def test_status_transitions_valid_and_invalid(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    ok = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={"status": "queued", "payment": {"cashAmount": 100.0}},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "queued"

    # The shop floor's move: starting the cut is what takes the order out of the queue.
    ok2 = client.patch(
        f"/api/v1/orders/{oid}/activities/cutting", json={"status": "in_progress"}
    )
    assert ok2.status_code == 200
    assert ok2.json()["data"]["orderStatus"] == "in_process"
    # Accumulated history: creation + 2 transitions.
    detail = client.get(f"/api/v1/orders/{oid}").json()["data"]
    assert len(detail["history"]) == 3

    # in_process → dispatched skips a state: not a valid transition.
    bad = client.patch(f"/api/v1/orders/{oid}/status", json={"status": "dispatched"})
    assert bad.status_code == 422
    assert "inválida" in bad.json()["errors"][0]["message"]


def test_invalid_transition_from_confirmed(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    # confirmed → finished (skips states) is not valid.
    bad = client.patch(
        f"/api/v1/orders/{order['id']}/status", json={"status": "finished"}
    )
    assert bad.status_code == 422


def test_queued_requires_payment(client, db_session):
    """confirmed → queued without a payment method is blocked and the order doesn't advance."""
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    bad = client.patch(f"/api/v1/orders/{oid}/status", json={"status": "queued"})
    assert bad.status_code == 422
    assert "forma de pago" in bad.json()["errors"][0]["message"].lower()
    # The order stays confirmed (the transition wasn't applied).
    assert client.get(f"/api/v1/orders/{oid}").json()["data"]["status"] == "confirmed"

    # A payment present but with zero amounts isn't valid either.
    zero = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={
            "status": "queued",
            "payment": {"cashAmount": 0, "transferAmount": 0, "creditAmount": 0},
        },
    )
    assert zero.status_code == 422


def test_queued_records_payment(client, db_session):
    """Registers every method and freezes them on the order (informational)."""
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    ok = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={
            "status": "queued",
            "payment": {
                "cashAmount": 30.5,
                "transferAmount": 20.0,
                "creditAmount": 15.0,
            },
        },
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["status"] == "queued"
    assert data["paymentCashAmount"] == 30.5
    assert data["paymentTransferAmount"] == 20.0
    assert data["paymentCreditAmount"] == 15.0

    # Persisted: reads back the same.
    reread = client.get(f"/api/v1/orders/{oid}").json()["data"]
    assert reread["paymentCashAmount"] == 30.5
    assert reread["paymentTransferAmount"] == 20.0
    assert reread["paymentCreditAmount"] == 15.0


def test_queued_payment_single_method(client, db_session):
    """A single method (credit only) is valid; the others stay None."""
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    ok = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={"status": "queued", "payment": {"creditAmount": 80.0}},
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["paymentCreditAmount"] == 80.0
    assert data["paymentCashAmount"] is None
    assert data["paymentTransferAmount"] is None


def test_queued_payment_by_transfer_only(client, db_session):
    """Bank transfer alone passes the gate, like cash or credit alone."""
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    ok = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={"status": "queued", "payment": {"transferAmount": 120.0}},
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["status"] == "queued"
    assert data["paymentTransferAmount"] == 120.0
    assert data["paymentCashAmount"] is None
    assert data["paymentCreditAmount"] is None


def test_payment_reflected_in_the_document(client, db_session):
    """The registered payment prints as its own block on the order's document."""
    import io

    from pypdf import PdfReader

    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    client.patch(
        f"/api/v1/orders/{oid}/status",
        json={
            "status": "queued",
            "payment": {"cashAmount": 50.0, "transferAmount": 25.0},
        },
    )

    doc = client.get(f"/api/v1/orders/{oid}/document")
    assert doc.status_code == 200
    assert doc.headers["content-type"] == "application/pdf"
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(doc.content)).pages
    )
    assert "FORMA DE PAGO" in text
    assert "$50.00" in text and "$25.00" in text


def test_list_orders_filter_by_status(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))

    # Send the first one to production.
    client.patch(
        f"/api/v1/orders/{o1['id']}/status",
        json={"status": "queued", "payment": {"cashAmount": 100.0}},
    )

    in_prod = client.get("/api/v1/orders/", params={"status": "queued"}).json()
    assert [o["id"] for o in in_prod["data"]] == [o1["id"]]
    assert in_prod["meta"]["pagination"]["total"] == 1

    confirmed = client.get("/api/v1/orders/", params={"status": "confirmed"}).json()
    assert o1["id"] not in [o["id"] for o in confirmed["data"]]
    assert len(confirmed["data"]) == 1


def test_list_orders_filter_by_multiple_statuses(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))
    o3 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=400))

    # o1: confirmed → queued → in_process; o2: stays confirmed; o3: queued.
    _pay = {"payment": {"cashAmount": 100.0}}
    client.patch(f"/api/v1/orders/{o1['id']}/status", json={"status": "queued", **_pay})
    client.patch(
        f"/api/v1/orders/{o1['id']}/activities/cutting", json={"status": "in_progress"}
    )
    client.patch(f"/api/v1/orders/{o3['id']}/status", json={"status": "queued", **_pay})

    # Repeating the parameter filters by several statuses at once.
    resp = client.get(
        "/api/v1/orders/", params={"status": ["confirmed", "in_process"]}
    ).json()
    ids = {o["id"] for o in resp["data"]}
    assert ids == {o1["id"], o2["id"]}
    assert o3["id"] not in ids
    assert resp["meta"]["pagination"]["total"] == 2


def test_list_orders_is_fifo_oldest_first(client, db_session):
    """The workshop operates FIFO: orders list oldest to newest."""
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))
    o3 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=400))

    resp = client.get("/api/v1/orders/").json()
    assert [o["id"] for o in resp["data"]] == [o1["id"], o2["id"], o3["id"]]


def test_list_orders_sort_recent_reverses_the_fifo_order(client, db_session):
    """The back office reads newest first; the FIFO default stays untouched."""
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))
    o3 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=400))

    resp = client.get("/api/v1/orders/", params={"sort": "recent"}).json()
    assert [o["id"] for o in resp["data"]] == [o3["id"], o2["id"], o1["id"]]


def test_list_orders_search_by_code_id_and_client(client, db_session):
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
    o1 = _create_order(client, db_session, _order_payload(c1["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c2["id"], b["id"], width=500))

    # By order code (a fragment of it is enough).
    by_code = client.get("/api/v1/orders/", params={"search": o2["code"]}).json()
    assert [o["id"] for o in by_code["data"]] == [o2["id"]]

    # By client last name, case-insensitive.
    by_client = client.get("/api/v1/orders/", params={"search": "hopper"}).json()
    assert [o["id"] for o in by_client["data"]] == [o2["id"]]

    # By client identifier.
    by_ident = client.get("/api/v1/orders/", params={"search": "0100000017"}).json()
    assert [o["id"] for o in by_ident["data"]] == [o2["id"]]

    # An all-digit term also matches the order id exactly ("order 42").
    by_id = client.get("/api/v1/orders/", params={"search": str(o1["id"])}).json()
    assert o1["id"] in [o["id"] for o in by_id["data"]]

    # No match: empty page, and the total reflects the filter, not the table.
    none = client.get("/api/v1/orders/", params={"search": "zzzz"}).json()
    assert none["data"] == []
    assert none["meta"]["pagination"]["total"] == 0


def test_list_orders_filter_by_client(client, db_session):
    c1 = _create_client(client)
    c2 = _create_client(client, identifier="0100000017", phone="0100000017")
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c1["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c2["id"], b["id"], width=500))

    resp = client.get("/api/v1/orders/", params={"clientId": c2["id"]}).json()
    assert [o["id"] for o in resp["data"]] == [o2["id"]]
    assert o1["id"] not in [o["id"] for o in resp["data"]]
    assert resp["meta"]["pagination"]["total"] == 1


def test_list_orders_filter_by_created_day_range(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))

    # Backdate the first one; created_at is UTC-naive, so the range is a UTC day.
    db_session.query(OrderModel).filter(OrderModel.id == o1["id"]).update(
        {"created_at": datetime.utcnow() - timedelta(days=3)}
    )
    db_session.commit()

    today = datetime.utcnow().date()
    old_day = today - timedelta(days=3)

    # `createdTo` is inclusive: the backdated order's own day must return it.
    upto = client.get(
        "/api/v1/orders/", params={"createdTo": old_day.isoformat()}
    ).json()
    assert [o["id"] for o in upto["data"]] == [o1["id"]]

    # `createdFrom` is inclusive too, and today's order is on today's boundary.
    since = client.get(
        "/api/v1/orders/", params={"createdFrom": today.isoformat()}
    ).json()
    assert [o["id"] for o in since["data"]] == [o2["id"]]

    # Both ends together span everything.
    span = client.get(
        "/api/v1/orders/",
        params={"createdFrom": old_day.isoformat(), "createdTo": today.isoformat()},
    ).json()
    assert [o["id"] for o in span["data"]] == [o1["id"], o2["id"]]


def test_get_order_404(client):
    assert client.get("/api/v1/orders/999999").status_code == 404


def test_order_document_pdf_and_base64(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    pdf = client.get(f"/api/v1/orders/{oid}/document")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 1000

    # PDF/base64 are exempt from the envelope (file transport).
    b64 = client.get(f"/api/v1/orders/{oid}/document", params={"format": "base64"})
    assert b64.status_code == 200
    body = b64.json()
    assert body["format"] == "base64"
    assert body["mimeType"] == "application/pdf"
    assert order["code"] in body["filename"]


def test_order_document_renders_inline_material_without_label(client, db_session):
    """Regression: an inline (manual/offcut) material leaves product_code/product_name
    None; the document must render "N/A" instead of crashing on Paragraph(None) (was
    a 500)."""
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
                # label omitted -> product_name/product_code resolve to None
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
    order = _mint_order(db_session, payload)

    pdf = client.get(f"/api/v1/orders/{order.id}/document?format=pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 1000


def test_order_document_shows_all_configured_branches(client, db_session):
    """The letterhead lists every configured branch, not just the order's own
    branch (previously collapsed to a single one).

    Also the settings->letterhead pin: the company data is read live on every
    render, never frozen into the order's snapshot.
    """
    import io

    from pypdf import PdfReader

    client.patch(
        "/api/v1/settings/company",
        json={
            "branches": [
                {"name": "Sucursal Sucúa", "address": "Av. Principal 123"},
                {"name": "Sucursal Macas", "address": "Calle Secundaria 456"},
            ]
        },
    )
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    pdf = client.get(f"/api/v1/orders/{order['id']}/document")
    assert pdf.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
    )
    assert "Sucursal Sucúa" in text
    assert "Sucursal Macas" in text


def test_reference_is_printed_on_the_document(client, db_session):
    """``notes`` is the commercial reference (project/site): it must show up on
    the order's one document, escaped (``Paragraph`` parses mini-HTML)."""
    import io

    from pypdf import PdfReader

    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Proyecto Casa Pérez & Cía <cocina>"
    order = _create_order(client, db_session, payload)
    assert order["notes"] == "Proyecto Casa Pérez & Cía <cocina>"

    resp = client.get(f"/api/v1/orders/{order['id']}/document")
    assert resp.status_code == 200
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(resp.content)).pages
    )
    assert "Ref: Proyecto Casa Pérez & Cía <cocina>" in text


def test_order_listing_exposes_notes(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    payload = _order_payload(c["id"], b["id"])
    payload["notes"] = "Obra Los Álamos"
    _create_order(client, db_session, payload)

    assert client.get("/api/v1/orders/").json()["data"][0]["notes"] == "Obra Los Álamos"


def test_order_documents_404(client):
    assert client.get("/api/v1/orders/999999/document").status_code == 404


def test_order_export_document(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    resp = client.get(f"/api/v1/orders/{order['id']}/export")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["orderCode"] == order["code"]
    assert data["status"] == "confirmed"
    assert data["client"]["id"] == c["id"]
    assert data["currency"] == "USD"
    assert data["issuedAt"] is not None
    assert data["externalInvoiceId"] is None

    # Charge by product: one line with a readable description and the code.
    assert len(data["lines"]) == 1
    line = data["lines"][0]
    assert line["productCode"] == "MEL18"
    assert "MEL18" in line["description"]
    assert line["quantity"] == order["totalBoardsUsed"]
    assert line["unitPrice"] == 45.5
    assert line["lineTotal"] == line["quantity"] * 45.5
    assert data["subtotal"] == line["lineTotal"]
    assert data["taxAmount"] == round(data["subtotal"] * 0.15, 2)
    assert data["total"] == round(data["subtotal"] + data["taxAmount"], 2)


def test_set_external_invoice_id_and_reflect_in_export(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    resp = client.post(
        f"/api/v1/orders/{oid}/invoice", json={"externalInvoiceId": "FAC-001-42"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["externalInvoiceId"] == "FAC-001-42"

    # Idempotent with the same ID.
    again = client.post(
        f"/api/v1/orders/{oid}/invoice", json={"externalInvoiceId": "FAC-001-42"}
    )
    assert again.status_code == 200

    # The export reflects the associated invoice.
    exported = client.get(f"/api/v1/orders/{oid}/export").json()["data"]
    assert exported["externalInvoiceId"] == "FAC-001-42"


def test_set_external_invoice_id_conflict_on_different_id(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    oid = order["id"]

    client.post(
        f"/api/v1/orders/{oid}/invoice", json={"externalInvoiceId": "FAC-001-42"}
    )
    # A different ID on an already-invoiced order → 409 (doesn't overwrite the issued invoice).
    conflict = client.post(
        f"/api/v1/orders/{oid}/invoice", json={"externalInvoiceId": "FAC-999-00"}
    )
    assert conflict.status_code == 409


def test_billing_seam_404(client):
    assert client.get("/api/v1/orders/999999/export").status_code == 404
    assert (
        client.post(
            "/api/v1/orders/999999/invoice", json={"externalInvoiceId": "X"}
        ).status_code
        == 404
    )


def _manual_material_payload(client_id):
    """Order with a single 'manual' material (outside the catalog)."""
    return {
        "clientId": client_id,
        "branchId": _BRANCH,
        "materials": [
            {
                "key": "m1",
                "source": "manual",
                "height": 2000,
                "width": 1000,
                "thickness": 18,
                "costPerUnit": 30.0,
                "label": "Sobrante taller",
            }
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 1,
                "materialKey": "m1",
            }
        ],
    }


def test_create_order_with_non_catalog_material(client, db_session):
    """A 'manual' material freezes as-is in the snapshot: line without productId."""
    c = _create_client(client)

    data = _create_order(client, db_session, _manual_material_payload(c["id"]))

    # Charge = the manual material, identified by code/name (no productId).
    assert len(data["lines"]) == 1
    line = data["lines"][0]
    assert line["productId"] is None
    assert line["productCode"] == "m1"
    assert line["productName"] == "Sobrante taller"
    assert line["unitPriceSnapshot"] == 30.0
    assert line["lineTotal"] == 30.0 * line["quantity"]
    assert data["subtotal"] == 30.0 * data["totalBoardsUsed"]
    assert data["total"] == round(
        data["subtotal"] + round(data["subtotal"] * 0.15, 2), 2
    )

    # The piece cut from the manual material also has no productId.
    assert len(data["pieces"]) == 1
    assert data["pieces"][0]["productId"] is None

    # The order was persisted.
    assert len(client.get("/api/v1/orders/").json()["data"]) == 1


def test_create_mixed_catalog_and_offcut_order(client, db_session):
    """Mixed order: catalog board + company offcut (cost 0)."""
    c = _create_client(client)
    b = _create_board(client)

    payload = {
        "clientId": c["id"],
        "branchId": _BRANCH,
        "materials": [
            {"key": "b1", "source": "catalog", "productId": b["id"]},
            {
                "key": "r1",
                "source": "companyOffcut",
                "height": 1200,
                "width": 600,
                "thickness": 15,
                "costPerUnit": 0,
            },
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 1,
                "materialKey": "b1",
            },
            {
                "priority": 0,
                "height": 300,
                "width": 400,
                "quantity": 1,
                "materialKey": "r1",
            },
        ],
    }
    data = _create_order(client, db_session, payload)

    assert len(data["lines"]) == 2
    catalog_line = next(line for line in data["lines"] if line["productId"] is not None)
    offcut_line = next(line for line in data["lines"] if line["productId"] is None)
    assert catalog_line["productId"] == b["id"]
    assert catalog_line["productCode"] == "MEL18"
    assert offcut_line["productCode"] == "r1"
    # The zero-cost offcut doesn't add to the total; it = only the catalog board.
    assert offcut_line["lineTotal"] == 0
    assert data["subtotal"] == catalog_line["lineTotal"]
    assert data["total"] == round(
        data["subtotal"] + round(data["subtotal"] * 0.15, 2), 2
    )

    # Each piece references its material: catalog → productId, offcut → None.
    piece_product_ids = {p["productId"] for p in data["pieces"]}
    assert piece_product_ids == {b["id"], None}


def test_non_catalog_order_renders_its_document(client, db_session):
    """The document renders from the snapshot alone, without the catalog."""
    c = _create_client(client)
    order = _create_order(client, db_session, _manual_material_payload(c["id"]))

    document = client.get(f"/api/v1/orders/{order['id']}/document")
    assert document.status_code == 200
    assert document.headers["content-type"] == "application/pdf"
    assert len(document.content) > 1000


def test_order_freezes_chosen_packing_strategy(client, db_session):
    """The chosen strategy freezes into the order's immutable snapshot."""
    b = _create_board(client)
    c1 = _create_client(client, identifier="0100000371", phone="0100000371")
    order = _mint_order(
        db_session, _order_payload(c1["id"], b["id"], strategy="longOffcuts")
    )
    assert order.optimization_snapshot["strategy"] == "longOffcuts"

    # Omitting the strategy freezes the default behavior.
    c2 = _create_client(client, identifier="0100000389", phone="0100000389")
    order_default = _mint_order(db_session, _order_payload(c2["id"], b["id"]))
    assert order_default.optimization_snapshot["strategy"] == "default"


def test_order_freezes_whole_board_line_and_plan(client, db_session):
    """The same small job, sold whole: full price, full sheet, half handed over.

    The mirror of ``test_order_freezes_half_board_line_and_plan``: the client
    asked for the entire board, so the line, the physical board and the leftover
    the workshop sees all have to say so.
    """
    c = _create_client(client)
    b = _create_board(client)  # 2440×1220, price 45.5

    payload = _order_payload(c["id"], b["id"], height=300, width=300, quantity=1)
    payload["materials"][0]["wholeBoard"] = True
    data = _create_order(client, db_session, payload)

    line = data["lines"][0]
    assert line["halfBoard"] is False
    assert line["unitPriceSnapshot"] == 45.5
    assert line["lineTotal"] == 45.5
    assert not line["productName"].endswith("(medio tablero)")
    assert data["subtotal"] == 45.5
    assert data["total"] == 52.33

    plan = client.get(f"/api/v1/orders/{data['id']}/cutting-plan").json()["data"]
    board = plan["boards"][0]
    assert board["halfBoard"] is False
    assert board["width"] == 1220
    # The uncut half reaches the workshop as one leftover, with the rip cut that
    # frees it — no new column, it rides in the snapshot.
    assert {"x": 610.0, "y": 0.0, "width": 610.0, "height": 2440.0} in [
        {k: float(v) for k, v in r.items()} for r in board["remainders"]
    ]


def test_two_orders_differing_only_in_whole_board_are_not_duplicates(
    client, db_session
):
    """``wholeBoard`` isn't in the hash, so the subtotal is what separates them."""
    c = _create_client(client)
    b = _create_board(client)

    plain = _order_payload(c["id"], b["id"], height=300, width=300, quantity=1)
    whole = _order_payload(c["id"], b["id"], height=300, width=300, quantity=1)
    whole["materials"][0]["wholeBoard"] = True

    half_order = _mint_order(db_session, plain)
    whole_order = _mint_order(db_session, whole)
    assert half_order.id != whole_order.id
    assert half_order.optimization_hash == whole_order.optimization_hash

    # Idempotency itself is untouched: the same request still dedupes.
    assert _mint_order(db_session, whole).id == whole_order.id


def test_order_freezes_half_board_line_and_plan(client, db_session):
    """A small catalog job freezes the line and the board as a half."""
    c = _create_client(client)
    b = _create_board(client)  # 2440×1220, price 45.5

    # A single small piece → fits in a half board (width 610).
    payload = _order_payload(c["id"], b["id"], height=300, width=300, quantity=1)
    data = _create_order(client, db_session, payload)

    # Charged at half price, labeled as half board.
    assert len(data["lines"]) == 1
    line = data["lines"][0]
    assert line["halfBoard"] is True
    assert line["quantity"] == 1
    assert line["unitPriceSnapshot"] == 25.03  # price/2 * 1.10 (default markup)
    assert line["lineTotal"] == 25.03
    assert line["productName"].endswith("(medio tablero)")
    assert data["subtotal"] == 25.03
    assert data["total"] == 28.78

    # Cutting plan: the physical board is a half (width/2), flagged.
    plan = client.get(f"/api/v1/orders/{data['id']}/cutting-plan").json()["data"]
    assert len(plan["boards"]) == 1
    board = plan["boards"][0]
    assert board["halfBoard"] is True
    assert board["width"] == 610

    # The document (ORDEN DE PEDIDO) is generated with the half-board line.
    doc = client.get(f"/api/v1/orders/{data['id']}/document")
    assert doc.status_code == 200
    assert doc.headers["content-type"] == "application/pdf"


def test_create_order_with_pooled_offcut_freezes_zero_cost_line(client, db_session):
    """An order over a catalog board + a client offcut freezes both, the offcut at $0."""
    c = _create_client(client)
    b = _create_board(client)

    payload = {
        "clientId": c["id"],
        "branchId": _BRANCH,
        "materials": [
            {
                "key": "b1",
                "source": "catalog",
                "productId": b["id"],
                "fillOrder": "offcutsFirst",
            },
            {
                "key": "off1",
                "source": "clientOffcut",
                "height": 400,
                "width": 600,
                "thickness": 18,
                "costPerUnit": 0,
                "quantity": 1,
                "poolKey": "b1",
                "label": "Retazo cliente",
            },
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 200,
                "width": 300,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Chico",
                "canRotate": True,
            },
            {
                "priority": 0,
                "height": 2000,
                "width": 1000,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Grande",
                "canRotate": True,
            },
        ],
    }

    data = _create_order(client, db_session, payload)

    assert data["status"] == "confirmed"
    # Only the catalog board counts as a board bought.
    assert data["totalBoardsUsed"] == 1

    lines_by_code = {line["productCode"]: line for line in data["lines"]}
    # The client's offcut is frozen as its own $0 line, without a catalog product.
    offcut_line = lines_by_code["off1"]
    assert offcut_line["productId"] is None
    assert offcut_line["lineTotal"] == 0
    # The catalog board is billed as usual.
    assert lines_by_code["MEL18"]["productId"] == b["id"]

    # Cut list keeps both pieces, mapped to the catalog product (same material).
    assert {p["label"] for p in data["pieces"]} == {"Chico", "Grande"}
    assert all(p["productId"] == b["id"] for p in data["pieces"])


def test_create_order_on_client_offcuts_only(client, db_session):
    """A job cut entirely on the client's material: no board bought, $0 of material.

    What the shop charges lives in the additional services; the order still has to
    carry the full cut plan so the workshop can produce it.
    """
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
                "quantity": 2,
                "materialKey": "r1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
        "additionalServices": [
            {"name": "Servicio de corte", "unitPrice": 20.0, "quantity": 1}
        ],
    }

    data = _create_order(client, db_session, payload)

    assert data["status"] == "confirmed"
    # The client brought the material: nothing was bought and nothing is billed for it.
    assert data["totalBoardsUsed"] == 0
    assert all(line["lineTotal"] == 0 for line in data["lines"])
    assert all(line["productId"] is None for line in data["lines"])
    # Both retazos are cut, one piece each, and the pieces carry no catalog product.
    assert {line["productCode"] for line in data["lines"]} == {"r1", "r2"}
    # One `order_pieces` row per requirement, so the pair rides as quantity 2.
    assert [p["quantity"] for p in data["pieces"]] == [2]
    assert all(p["productId"] is None for p in data["pieces"])
    # The bill is the service alone.
    # The service is registered tax-included and folded into the net subtotal.
    assert data["subtotal"] == round(20.0 / (1 + data["taxRate"]), 2)

    # The document renders from this snapshot.
    resp = client.get(f"/api/v1/orders/{data['id']}/document")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


# --- The status clock in the listing -------------------------------------------
def _pay():
    return {"payment": {"cashAmount": 100.0}}


def test_listing_exposes_the_status_clock(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    row = client.get("/api/v1/orders/").json()["data"][0]
    born = row["statusChangedAt"]
    assert born is not None
    # No banding on this order, so it carries the cut activity and nothing else.
    assert [a["type"] for a in row["activities"]] == ["cutting"]

    client.patch(
        f"/api/v1/orders/{order['id']}/status", json={"status": "queued", **_pay()}
    )
    moved = client.get("/api/v1/orders/").json()["data"][0]["statusChangedAt"]
    assert moved > born


def test_status_clock_is_not_restarted_by_prioritizing(client, db_session):
    """Flagging an order urgent must not make it look freshly moved.

    ``set_priority`` writes a history row with ``from == to``; if the clock keyed
    on history rather than on real transitions, marking the stuck order would be
    what hides it.
    """
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))
    before = client.get(f"/api/v1/orders/{order['id']}").json()["data"][
        "statusChangedAt"
    ]

    resp = client.patch(
        f"/api/v1/orders/{order['id']}/priority", json={"isPriority": True}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["statusChangedAt"] == before


def test_list_orders_sort_stalest_puts_closed_orders_last(client, db_session):
    """The control view: longest sitting first, and the closed ones out of the way.

    ``o1`` is moved last, so by creation order it is the FIRST but by clock it is
    the FRESHEST — which is exactly what separates this sort from ``oldest``.
    """
    c = _create_client(client)
    b = _create_board(client)
    o1 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=600))
    o2 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=500))
    o3 = _create_order(client, db_session, _order_payload(c["id"], b["id"], width=400))

    # o3 is cancelled (closed → last), then o1 moves (→ freshest of the live ones).
    client.patch(f"/api/v1/orders/{o3['id']}/status", json={"status": "cancelled"})
    client.patch(
        f"/api/v1/orders/{o1['id']}/status", json={"status": "queued", **_pay()}
    )

    resp = client.get("/api/v1/orders/", params={"sort": "stalest"}).json()
    assert [o["id"] for o in resp["data"]] == [o2["id"], o1["id"], o3["id"]]


def test_list_orders_filter_by_activity(client, db_session):
    """An order with no canto has no banding row at all, so it matches neither filter."""
    c = _create_client(client)
    b = _create_board(client)
    order = _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    cutting = client.get(
        "/api/v1/orders/", params={"activity": "cutting", "activityStatus": "pending"}
    ).json()
    assert [o["id"] for o in cutting["data"]] == [order["id"]]

    # The type alone: "orders that carry this activity at all".
    banding = client.get("/api/v1/orders/", params={"activity": "banding"}).json()
    assert banding["data"] == []
    assert banding["meta"]["pagination"]["total"] == 0


def test_timestamps_are_serialized_as_utc(client, db_session):
    """Every datetime on the wire carries its zone, or the browser guesses wrong.

    The columns are naive ``datetime.utcnow()``; a naive ISO string is read by
    ``new Date(...)`` as LOCAL time, which in Ecuador (UTC-5) silently shifts
    every elapsed-time reading by five hours. Pinning it here because the fix is
    one serializer on ``CamelModel`` and nothing else would notice its removal.
    """
    c = _create_client(client)
    b = _create_board(client)
    _create_order(client, db_session, _order_payload(c["id"], b["id"]))

    row = client.get("/api/v1/orders/").json()["data"][0]
    for field in ("createdAt", "confirmedAt", "statusChangedAt"):
        assert row[field].endswith("Z"), f"{field} = {row[field]!r}"
    # Nested models inherit it, and keep their camelCase aliases.
    assert row["history"][0]["createdAt"].endswith("Z")


def test_two_orders_differing_only_in_skip_trim_are_not_duplicates(client, db_session):
    """This flag IS in the hash, so the dedupe separates them without help.

    The mirror of ``wholeBoard``, which needs the subtotal to tell two orders
    apart because it never reaches the hash.
    """
    c = _create_client(client)
    b = _create_board(client)

    trimmed = _order_payload(c["id"], b["id"])
    untrimmed = _order_payload(c["id"], b["id"])
    untrimmed["materials"][0]["skipTrim"] = True

    first = _mint_order(db_session, trimmed)
    second = _mint_order(db_session, untrimmed)
    assert first.id != second.id
    assert first.optimization_hash != second.optimization_hash

    # Idempotency is untouched.
    assert _mint_order(db_session, untrimmed).id == second.id


def test_order_snapshot_keeps_the_refilado_decision(client, db_session):
    """The order freezes it, so the documents keep saying "sin refilar" forever."""
    c = _create_client(client)
    b = _create_board(client)

    payload = _order_payload(c["id"], b["id"])
    payload["materials"][0]["skipTrim"] = True
    order = _mint_order(db_session, payload)

    materials = order.optimization_snapshot["materials"]
    assert materials[0]["skip_trim"] is True
    assert order.optimization_snapshot["materials_summary"][0]["skip_trim"] is True

    # And the PDF still renders with the mark in it.
    resp = client.get(f"/api/v1/orders/{order.id}/document")
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"
