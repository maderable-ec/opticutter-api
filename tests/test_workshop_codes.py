"""The workshop codes of the cut list, end to end (abisagrado, ensamble, ranurado).

The seller types them on the cut list; the order freezes them on its pieces and
on every placed instance; the operator reads them on the cutting plan, the
bander's additional work is measured against them, and the ORDEN DE PEDIDO
prints them. The pure rules (naming, hash, PDF column) are pinned in
``tests/unit/test_workshop_codes.py``; this covers what only real rows prove.
"""

import io

from pypdf import PdfReader

from tests.order_helpers import (
    _BRANCH,
    _activity,
    _create_board,
    _create_client,
    _cut_piece,
    _mint_order,
    _order_with_services,
    _to_in_process,
    _worked_pieces,
)


def _plan_pieces(client, oid):
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    return [p for board in plan["boards"] for p in board["pieces"]]


def test_the_order_freezes_the_codes_on_the_cut_list(client, db_session):
    order = _order_with_services(client, db_session)
    by_label = {p["label"]: p for p in order["pieces"]}
    assert by_label["Fondo"]["hingingCode"] == "B2"
    assert by_label["Fondo"]["groovingCode"] == "R1"
    assert by_label["Fondo"]["assemblyCode"] is None
    assert by_label["Costado"]["hingingCode"] is None


def test_every_instance_of_a_row_takes_its_codes(client, db_session):
    """``quantity: 2`` is two placed pieces, and both carry the work."""
    order = _order_with_services(client, db_session)
    pieces = _plan_pieces(client, order["id"])
    fondos = [p for p in pieces if p["label"] == "Fondo"]
    costados = [p for p in pieces if p["label"] == "Costado"]
    assert len(fondos) == 2
    assert all(p["hingingCode"] == "B2" and p["groovingCode"] == "R1" for p in fondos)
    assert all(p["hingingCode"] is None for p in costados)


def test_a_piece_on_a_pooled_retazo_keeps_its_codes(client, db_session):
    """Cut on the second retazo of a pool, named after the anchor's cut list."""
    c = _create_client(client, identifier="0100000470")
    retazo = {
        "source": "clientOffcut",
        "height": 700,
        "width": 700,
        "thickness": 18,
    }
    order = _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [
                {"key": "r1", **retazo},
                {"key": "r2", **retazo, "poolKey": "r1"},
            ],
            "requirements": [
                {
                    "priority": 0,
                    "height": 500,
                    "width": 500,
                    "quantity": 2,
                    "materialKey": "r1",
                    "label": "Tapa",
                    "canRotate": True,
                    "assemblyCode": "E7",
                }
            ],
        },
    )
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    assert {b["materialKey"] for b in plan["boards"]} == {"r1", "r2"}
    pieces = [p for board in plan["boards"] for p in board["pieces"]]
    assert len(pieces) == 2
    assert all(p["assemblyCode"] == "E7" for p in pieces)


def test_the_board_counts_the_additional_work_on_its_own_pieces(client, db_session):
    order = _order_with_services(client, db_session)
    _to_in_process(client, order["id"])
    assert (
        _cut_piece(
            client, order["id"], _worked_pieces(client, order["id"])[0]
        ).status_code
        == 200
    )
    queue = client.get("/api/v1/orders/workshop-queue").json()["data"]
    card = next(item for item in queue if item["orderId"] == order["id"])
    assert _activity(card, "additional")["progress"] == {
        "cutPieces": 1,
        "totalPieces": 2,
    }
    assert _activity(card, "banding")["progress"] == {
        "cutPieces": 0,
        "totalPieces": 2,
    }
    assert card["progress"] == {"cutPieces": 1, "totalPieces": 4}


def test_the_codes_survive_a_quote_confirmed_by_the_client(client, db_session):
    """The pre-order stores them raw; the confirmed order inherits them."""
    c = _create_client(client, identifier="0100000488")
    b = _create_board(client, code="MEL0488")
    pre = client.post(
        "/api/v1/preorders/",
        json={
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [
                {
                    "priority": 0,
                    "height": 800,
                    "width": 700,
                    "quantity": 1,
                    "materialKey": "b1",
                    "label": "Puerta",
                    "hingingCode": "  B4 ",
                }
            ],
        },
    ).json()["data"]
    detail = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    assert detail["requirements"][0]["hingingCode"] == "B4"

    token = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"][
        "token"
    ]
    confirmed = client.post(f"/api/v1/public/review/{token}/confirm")
    assert confirmed.status_code == 200
    order_id = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]["orderId"]
    order = client.get(f"/api/v1/orders/{order_id}").json()["data"]
    assert order["pieces"][0]["hingingCode"] == "B4"
    assert _activity(order, "additional") is not None


def test_the_orden_de_pedido_prints_the_codes(client, db_session):
    order = _order_with_services(client, db_session)
    resp = client.get(f"/api/v1/orders/{order['id']}/document")
    assert resp.status_code == 200
    reader = PdfReader(io.BytesIO(resp.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Taller" in text
    assert "Abis B2" in text
