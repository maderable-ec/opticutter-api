"""Integration tests for order attachments (anexos).

PDFs/screenshots attached to an order while it is still open (not
completed/dispatched/cancelled). Upload/delete = admin+seller (orders:write);
list/download = anyone who reads the order (orders:read). Bytes live on local
disk under ``config.ATTACHMENTS_DIR`` (pointed at a tmp dir here); only metadata
lives in Postgres.
"""

import io

import pytest
from PIL import Image

from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.config import config

_PWD = "pw-supersecret"
_BRANCH = 1  # default branch seeded by conftest


@pytest.fixture(autouse=True)
def _attachments_dir(tmp_path, monkeypatch):
    """Point attachment storage at an isolated tmp dir (no repo pollution)."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", str(tmp_path))
    return tmp_path


# --- order/user helpers ---------------------------------------------------------
def _create_client(client, identifier="0991112233"):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": identifier,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": identifier,
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


def _order_payload(client_id, product_id, width=600):
    return {
        "clientId": client_id,
        "branchId": _BRANCH,
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": width,
                "quantity": 2,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }


def _mint_order(client, db_session, identifier="0991112233", code="MEL18", width=600):
    c = _create_client(client, identifier=identifier)
    b = _create_board(client, code=code)
    order = OrderService(db_session).create(
        OrderCreate.model_validate(_order_payload(c["id"], b["id"], width=width))
    )
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def _token_for(client, db_session, role, email=None):
    email = email or f"{role}@empresa.com"
    svc = UserService(db_session)
    if svc.get_by_email(email) is None:
        svc.create(
            UserCreate(
                email=email,
                password=_PWD,
                role=role,
                full_name=role.title(),
                branch_id=None if role == "administrador" else _BRANCH,
            )
        )
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": _PWD}
    ).json()["data"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _to_completed(client, oid):
    """Drives the order (no edge banding) up to 'completed' as admin."""
    client.patch(
        f"/api/v1/orders/{oid}/status",
        json={"status": "queued", "payment": {"cashAmount": 100.0}},
    )
    client.patch(f"/api/v1/orders/{oid}/status", json={"status": "cutting"})
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    for board in plan["boards"]:
        for piece in board["pieces"]:
            client.patch(
                f"/api/v1/orders/{oid}/cutting-plan/pieces/{piece['id']}",
                json={"cut": True},
            )
    client.patch(f"/api/v1/orders/{oid}/status", json={"status": "cut"})
    client.patch(f"/api/v1/orders/{oid}/status", json={"status": "completed"})


# --- file helpers ---------------------------------------------------------------
_PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


def _png_bytes(color="red"):
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(buf, "PNG")
    return buf.getvalue()


def _upload(client, oid, name, data, content_type, headers=None):
    return client.post(
        f"/api/v1/orders/{oid}/attachments",
        files={"file": (name, data, content_type)},
        headers=headers or {},
    )


# --------------------------------------------------------------------------- #
# Upload / list / download
# --------------------------------------------------------------------------- #
def test_upload_pdf_list_and_download(client, db_session):
    order = _mint_order(client, db_session)
    oid = order["id"]

    resp = _upload(client, oid, "cotizacion.pdf", _PDF_BYTES, "application/pdf")
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["filename"] == "cotizacion.pdf"
    assert data["contentType"] == "application/pdf"
    assert data["sizeBytes"] == len(_PDF_BYTES)
    att_id = data["id"]

    listed = client.get(f"/api/v1/orders/{oid}/attachments").json()["data"]
    assert [a["id"] for a in listed] == [att_id]

    dl = client.get(f"/api/v1/orders/{oid}/attachments/{att_id}")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    assert dl.content == _PDF_BYTES


def test_upload_png_screenshot(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000001", code="MELP")
    png = _png_bytes()
    resp = _upload(client, order["id"], "captura.png", png, "image/png")
    assert resp.status_code == 201
    assert resp.json()["data"]["contentType"] == "image/png"


def test_download_filename_with_non_latin1_chars(client, db_session):
    """macOS screenshots embed U+202F (narrow no-break space): not latin-1 encodable.

    The download must not 500 building the Content-Disposition header (regression).
    """
    order = _mint_order(client, db_session, identifier="0990000014", code="MELU")
    oid = order["id"]
    weird = "Captura de pantalla.png"  # U+202F, as Finder names them
    png = _png_bytes()
    att = _upload(client, oid, weird, png, "image/png").json()["data"]
    assert att["filename"] == weird

    dl = client.get(f"/api/v1/orders/{oid}/attachments/{att['id']}")
    assert dl.status_code == 200
    assert dl.content == png
    # RFC 5987 form carries the real UTF-8 name; the ASCII fallback stays valid.
    assert "filename*=UTF-8''" in dl.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_reject_unsupported_type(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000002", code="MELT")
    resp = _upload(client, order["id"], "notas.txt", b"hola", "text/plain")
    assert resp.status_code == 422


def test_reject_corrupt_image(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000003", code="MELC")
    resp = _upload(client, order["id"], "fake.png", b"not-a-real-image", "image/png")
    assert resp.status_code == 422


def test_reject_oversize(client, db_session, monkeypatch):
    monkeypatch.setattr(config, "MAX_ATTACHMENT_MB", 1)
    order = _mint_order(client, db_session, identifier="0990000004", code="MELO")
    big = b"%PDF-1.4" + b"0" * (2 * 1024 * 1024)  # ~2 MB, over the 1 MB cap
    resp = _upload(client, order["id"], "grande.pdf", big, "application/pdf")
    assert resp.status_code == 422


def test_reject_empty_file(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000005", code="MELE")
    resp = _upload(client, order["id"], "vacio.pdf", b"", "application/pdf")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Terminal-state gate: no attach/delete once the order is closed
# --------------------------------------------------------------------------- #
def test_cannot_attach_when_completed(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000006", code="MELX")
    _to_completed(client, order["id"])
    resp = _upload(client, order["id"], "tarde.pdf", _PDF_BYTES, "application/pdf")
    assert resp.status_code == 422


def test_cannot_delete_when_completed(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000007", code="MELY")
    oid = order["id"]
    att_id = _upload(client, oid, "a.pdf", _PDF_BYTES, "application/pdf").json()[
        "data"
    ]["id"]
    _to_completed(client, oid)
    resp = client.delete(f"/api/v1/orders/{oid}/attachments/{att_id}")
    assert resp.status_code == 422
    # It's still listed (the delete was rejected).
    listed = client.get(f"/api/v1/orders/{oid}/attachments").json()["data"]
    assert [a["id"] for a in listed] == [att_id]


# --------------------------------------------------------------------------- #
# Delete (happy path) + not-found
# --------------------------------------------------------------------------- #
def test_delete_attachment(client, db_session, _attachments_dir):
    order = _mint_order(client, db_session, identifier="0990000008", code="MELD")
    oid = order["id"]
    att = _upload(client, oid, "borrar.pdf", _PDF_BYTES, "application/pdf").json()[
        "data"
    ]
    # The file exists on disk (one file under the order's subfolder).
    order_dir = _attachments_dir / str(oid)
    assert order_dir.exists() and len(list(order_dir.iterdir())) == 1

    resp = client.delete(f"/api/v1/orders/{oid}/attachments/{att['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/v1/orders/{oid}/attachments").json()["data"] == []
    # The file is gone too.
    assert list(order_dir.iterdir()) == []


def test_attachment_not_found(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000009", code="MELN")
    assert (
        client.get(f"/api/v1/orders/{order['id']}/attachments/999999").status_code
        == 404
    )


def test_attachment_of_another_order_is_404(client, db_session):
    o1 = _mint_order(client, db_session, identifier="0990000010", code="MELA1")
    o2 = _mint_order(client, db_session, identifier="0990000011", code="MELA2")
    att = _upload(client, o1["id"], "x.pdf", _PDF_BYTES, "application/pdf").json()[
        "data"
    ]
    # The attachment belongs to o1: fetching it under o2 must 404.
    resp = client.get(f"/api/v1/orders/{o2['id']}/attachments/{att['id']}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# RBAC: upload/delete are admin+seller; operator can read but not write
# --------------------------------------------------------------------------- #
def test_operator_cannot_upload_but_can_read(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000012", code="MELR")
    oid = order["id"]
    # Seed an attachment as admin so the operator has something to read.
    _upload(client, oid, "ref.pdf", _PDF_BYTES, "application/pdf")

    headers = _token_for(client, db_session, "operador")
    # Upload is forbidden for the operator (orders:write = admin/seller).
    denied = _upload(client, oid, "op.pdf", _PDF_BYTES, "application/pdf", headers)
    assert denied.status_code == 403
    # But listing (orders:read) works.
    listed = client.get(f"/api/v1/orders/{oid}/attachments", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()["data"]) == 1


def test_seller_can_upload(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000013", code="MELS")
    headers = _token_for(client, db_session, "vendedor")
    resp = _upload(client, order["id"], "v.pdf", _PDF_BYTES, "application/pdf", headers)
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Consolidated print PDF
# --------------------------------------------------------------------------- #
def _real_pdf_bytes():
    """A genuine 1-page PDF (pypdf-parseable), unlike the minimal _PDF_BYTES stub."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 72, "anexo de prueba")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _page_count(pdf_bytes):
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def _all_text(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_consolidated_has_diagram_only_no_repeated_lists(client, db_session):
    """The despiece section is the gráfico only: the order carries the piece/board
    lists, so the production-sheet lists must NOT reappear in the packet.

    The diagram pages carry no heading of their own either — they are bare
    landscape sheets, one pattern each — so they are identified here by their
    orientation and embedded image rather than by a title.
    """
    from pypdf import PdfReader

    order = _mint_order(client, db_session, identifier="0990000023", code="MELDIAG")
    content = client.get(f"/api/v1/orders/{order['id']}/consolidated").content
    text = _all_text(content)

    # The two documents that do carry a title are present.
    assert "ORDEN DE PEDIDO" in text
    assert "HOJA DE DESPACHO" in text
    # The production sheet's repeated lists are gone (they live in the order).
    assert "LISTA DE CORTE" not in text
    assert "TABLEROS A UTILIZAR" not in text

    pages = PdfReader(io.BytesIO(content)).pages
    diagram_pages = [
        page
        for page in pages
        if float(page.mediabox.width) > float(page.mediabox.height)
    ]
    # At least one landscape sheet, and every one of them holds a diagram.
    assert diagram_pages
    assert all(len(page.images) for page in diagram_pages)


def test_consolidated_pdf_includes_attachments(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000020", code="MELCON")
    oid = order["id"]

    # Baseline: the three base documents (order + production + dispatch), no annexes.
    base = client.get(f"/api/v1/orders/{oid}/consolidated")
    assert base.status_code == 200
    assert base.headers["content-type"] == "application/pdf"
    base_pages = _page_count(base.content)
    assert base_pages >= 3  # at least one page per base document

    # Add a real PDF (1 page) and a PNG screenshot (wrapped to 1 page).
    _upload(client, oid, "cotiz.pdf", _real_pdf_bytes(), "application/pdf")
    _upload(client, oid, "captura.png", _png_bytes(), "image/png")

    full = client.get(f"/api/v1/orders/{oid}/consolidated")
    assert full.status_code == 200
    # Exactly two more pages: the PDF annex (1) + the image annex (1).
    assert _page_count(full.content) == base_pages + 2


def test_consolidated_pdf_base64(client, db_session):
    order = _mint_order(client, db_session, identifier="0990000021", code="MELB64")
    resp = client.get(
        f"/api/v1/orders/{order['id']}/consolidated", params={"format": "base64"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "base64"
    assert body["mimeType"] == "application/pdf"
    assert "consolidado" in body["filename"]


def test_consolidated_skips_corrupt_pdf_annex(client, db_session):
    """A corrupt PDF annex is skipped, not fatal: the packet still renders."""
    order = _mint_order(client, db_session, identifier="0990000022", code="MELCOR")
    oid = order["id"]
    base_pages = _page_count(client.get(f"/api/v1/orders/{oid}/consolidated").content)

    # _PDF_BYTES is a structurally invalid PDF (no page tree): upload succeeds
    # (type/size only), but the consolidated merge must skip it gracefully.
    _upload(client, oid, "roto.pdf", _PDF_BYTES, "application/pdf")

    resp = client.get(f"/api/v1/orders/{oid}/consolidated")
    assert resp.status_code == 200
    assert _page_count(resp.content) == base_pages  # annex skipped, no extra page
