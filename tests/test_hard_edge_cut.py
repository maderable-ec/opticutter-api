"""A hard tape cuts its piece 1 mm short per side: the plan, the hash, the order.

The seller types the FINAL size; the saw cuts it short on every side a hard
tape goes on (``hard_edges``). These tests hold the two sizes apart where each
belongs: the placed geometry is the cut, ``original_*`` and the cut list are
what was ordered, and a quote without a hard tape is untouched.
"""

import csv
import io
import xml.etree.ElementTree as ET

from src.modules.optimizations import hard_edges
from tests.order_helpers import _mint_order


def _create_client(client):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": "0100000397",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": "0100000397",
        },
    ).json()["data"]


def _create_board(client):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": "MEL18",
            "name": "Melamina MEL18",
            "price": 45.5,
            "attributes": {"height": 2440, "width": 1220, "thickness": 18},
        },
    ).json()["data"]


def _create_tape(client, code, band_type):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": code,
            "name": f"Tapacanto {code}",
            "price": 2.0,
            "attributes": {
                "thickness": 1.0 if band_type == "Hard" else 0.45,
                "width": 22,
                "length": 50000,
                "bandType": band_type,
            },
        },
    ).json()["data"]


def _request(client_id, board_id, tape_id, sides, height=600, width=400, **extra):
    return {
        "clientId": client_id,
        "materials": [{"key": "b1", "source": "catalog", "productId": board_id}],
        "requirements": [
            {
                "priority": 0,
                "height": height,
                "width": width,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Puerta",
                # Not rotatable, so the placed size reads in the piece's own frame.
                "canRotate": False,
                "edgeBanding": {"productId": tape_id, "sides": sides},
                **extra,
            }
        ],
    }


def _optimize(client, body):
    resp = client.post("/api/v1/optimize/", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _setup(client):
    return (
        _create_client(client)["id"],
        _create_board(client)["id"],
        _create_tape(client, "CD22", "Hard")["id"],
        _create_tape(client, "CS22", "Soft")["id"],
    )


def test_a_hard_tape_cuts_the_piece_short_and_keeps_the_ordered_size(client):
    client_id, board_id, hard, _ = _setup(client)

    # 2L CD: both long sides hard, 2 mm off the WIDTH; the largo stays.
    data = _optimize(client, _request(client_id, board_id, hard, ["left", "right"]))

    [piece] = data["layouts"][0]["placedPieces"]
    assert (piece["height"], piece["width"]) == (600, 398)
    assert (piece["originalHeight"], piece["originalWidth"]) == (600, 400)
    # The tape covers the FINAL edge: two sides of 600 mm.
    assert data["edgeBandingsSummary"][0]["netLinearM"] == 1.2


def test_a_short_side_takes_the_millimetre_off_the_largo(client):
    client_id, board_id, hard, soft = _setup(client)

    # The auto tape soft on the long sides, a hard canto especial on one short.
    body = _request(
        client_id,
        board_id,
        soft,
        ["left", "right"],
        specialEdges=[{"side": "top", "productId": hard}],
    )
    [piece] = _optimize(client, body)["layouts"][0]["placedPieces"]

    assert (piece["height"], piece["width"]) == (599, 400)
    assert (piece["originalHeight"], piece["originalWidth"]) == (600, 400)


def test_a_soft_tape_leaves_the_piece_and_the_hash_as_they_were(client, monkeypatch):
    client_id, board_id, hard, soft = _setup(client)
    soft_body = _request(client_id, board_id, soft, ["left", "right", "top"])
    hard_body = _request(client_id, board_id, hard, ["left", "right", "top"])

    soft_data = _optimize(client, soft_body)
    hard_data = _optimize(client, hard_body)
    [piece] = soft_data["layouts"][0]["placedPieces"]
    assert (piece["height"], piece["width"]) == (600, 400)

    # With the rule off, a soft quote hashes exactly as it does with it on (its
    # Redis entry survives the deploy); a hard one does not.
    monkeypatch.setattr(hard_edges, "HARD_EDGE_CUT_MM", 0)
    assert (
        _optimize(client, soft_body)["optimizationHash"]
        == (soft_data["optimizationHash"])
    )
    assert (
        _optimize(client, hard_body)["optimizationHash"]
        != (hard_data["optimizationHash"])
    )


def test_a_piece_left_out_is_reported_at_the_size_the_seller_typed(client):
    client_id, board_id, hard, _ = _setup(client)

    # 2500 tall on a 2440 board: still too tall cut 2 mm short.
    body = _request(client_id, board_id, hard, ["top", "bottom"], height=2500)
    [entry] = _optimize(client, body)["unplaced"]

    assert (entry["height"], entry["width"]) == (2500, 400)
    assert (entry["cutHeight"], entry["cutWidth"]) == (2498, 400)
    assert entry["reason"] == "larger_than_sheet"


def test_the_hard_tape_can_make_a_piece_fit(client):
    client_id, board_id, hard, soft = _setup(client)

    # 2442 does not fit a 2440 board; cut 2 mm short it does, exactly.
    tall = {"height": 2442, "width": 400}
    soft_data = _optimize(
        client, _request(client_id, board_id, soft, ["top", "bottom"], **tall)
    )
    hard_data = _optimize(
        client, _request(client_id, board_id, hard, ["top", "bottom"], **tall)
    )

    assert soft_data["unplaced"] and not hard_data["unplaced"]
    [piece] = hard_data["layouts"][0]["placedPieces"]
    assert piece["height"] == 2440 and piece["originalHeight"] == 2442


def test_the_editor_places_the_cut_size_and_names_the_ordered_one(client):
    client_id, board_id, hard, _ = _setup(client)
    body = _request(client_id, board_id, hard, ["left"])

    resp = client.post("/api/v1/optimize/layout/evaluate", json=body)

    assert resp.status_code == 200, resp.text
    [piece] = resp.json()["data"]["pools"][0]["pieces"]
    assert (piece["height"], piece["width"]) == (600, 399)
    assert (piece["originalHeight"], piece["originalWidth"]) == (600, 400)


def test_the_order_freezes_both_sizes_and_exports_the_cut(client, db_session):
    client_id, board_id, hard, _ = _setup(client)
    order = _mint_order(
        client,
        db_session,
        {"branchId": 1, **_request(client_id, board_id, hard, ["left", "right"])},
    )

    # The cut list keeps the ordered size ...
    [ordered] = order["pieces"]
    assert (ordered["height"], ordered["width"]) == (600, 400)
    # ... the operator's board draws and measures the cut ...
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    [placed] = plan["boards"][0]["pieces"]
    assert (placed["height"], placed["width"]) == (600, 398)
    assert (placed["originalHeight"], placed["originalWidth"]) == (600, 400)
    # ... and the shop's cutting program gets the cut, in both files.
    url = f"/api/v1/orders/{order['id']}/pieces/export"
    row = ET.fromstring(client.get(url, params={"format": "xml"}).content).find(
        "parts/row"
    )
    assert (row.find("length").text, row.find("width").text) == ("600", "398")
    rows = list(
        csv.reader(
            io.StringIO(
                client.get(url, params={"format": "csv"}).content.decode("utf-8-sig")
            )
        )
    )
    assert rows[1][1:3] == ["600", "398"]


def test_a_piece_the_seller_opted_out_is_cut_at_its_final_size(client):
    client_id, board_id, hard, _ = _setup(client)
    on = _request(client_id, board_id, hard, ["left", "right"])
    off = _request(client_id, board_id, hard, ["left", "right"], hardEdgeCut=False)

    on_data = _optimize(client, on)
    off_data = _optimize(client, off)
    [piece] = off_data["layouts"][0]["placedPieces"]
    assert (piece["height"], piece["width"]) == (600, 400)
    assert (piece["originalHeight"], piece["originalWidth"]) == (600, 400)
    # The tape is still hard: it still bands two sides of 600 mm.
    assert off_data["edgeBandingsSummary"][0]["netLinearM"] == 1.2
    assert off_data["optimizationHash"] != on_data["optimizationHash"]
    # On is the default, and the default is left out of the hash: saying it
    # explicitly quotes exactly what every existing quote does.
    explicit = _request(client_id, board_id, hard, ["left", "right"], hardEdgeCut=True)
    assert (
        _optimize(client, explicit)["optimizationHash"] == on_data["optimizationHash"]
    )


def test_the_order_freezes_the_opt_out_and_exports_the_final_size(client, db_session):
    client_id, board_id, hard, _ = _setup(client)
    body = _request(client_id, board_id, hard, ["left", "right"], hardEdgeCut=False)
    order = _mint_order(client, db_session, {"branchId": 1, **body})

    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    [placed] = plan["boards"][0]["pieces"]
    assert (placed["height"], placed["width"]) == (600, 400)
    # The export recomputes the cut off the frozen tapes: it must know.
    url = f"/api/v1/orders/{order['id']}/pieces/export"
    row = ET.fromstring(client.get(url, params={"format": "xml"}).content).find(
        "parts/row"
    )
    assert (row.find("length").text, row.find("width").text) == ("600", "400")
