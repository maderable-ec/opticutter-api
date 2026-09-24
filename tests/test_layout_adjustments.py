"""The seller's hand adjustments to the plan, end to end.

The editor's two endpoints, the lenient read every quote goes through, and the
path from a pre-order to the order that freezes the adjusted plan.
"""

from src.modules.orders.model import OrderModel
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService

from .test_orders import _create_board, _create_client

_BRANCH = 1


def _manual_request(quantity=4):
    """A hand-measured 1000x1000 sheet (no half board) and four 450x450 pieces:
    the optimizer lays them all on one sheet."""
    return {
        "materials": [
            {
                "key": "m1",
                "source": "manual",
                "height": 1000,
                "width": 1000,
                "thickness": 18,
                "costPerUnit": 40,
            }
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 450,
                "width": 450,
                "quantity": quantity,
                "materialKey": "m1",
                "label": "P",
            }
        ],
    }


def _evaluate(client, body):
    return client.post("/api/v1/optimize/layout/evaluate", json=body)


def _working_sheets(pool):
    """The pool's working sheets, as the editor sends them back."""
    return [
        {k: v for k, v in sheet.items() if k != "layout"} for sheet in pool["sheets"]
    ]


def _move_to_new_sheet(client, body, piece_id="P#4"):
    """Takes ``piece_id`` off its sheet and drops it on a new empty one, at the
    first position the server offers — the editor's own sequence of calls."""
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    sheets = _working_sheets(pool)
    for sheet in sheets:
        sheet["pieces"] = [p for p in sheet["pieces"] if p["pieceId"] != piece_id]
    sheets.append({"materialKey": pool["poolKey"], "halfBoard": False, "pieces": []})
    adjustments = [{"poolKey": pool["poolKey"], "sheets": sheets}]
    probe = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **body,
            "layoutAdjustments": adjustments,
            "probe": {
                "kind": "piece",
                "poolKey": pool["poolKey"],
                "pieceId": piece_id,
                "sheetIndex": len(sheets) - 1,
            },
        },
    )
    assert probe.status_code == 200, probe.text
    spot = probe.json()["data"]["sheets"][0]["positions"][0]
    sheets[-1]["pieces"].append(
        {
            "pieceId": piece_id,
            "x": spot["x"],
            "y": spot["y"],
            "rotated": spot["rotated"],
        }
    )
    return adjustments


# ---------------------------------------------------------------------------
# The editor's endpoints
# ---------------------------------------------------------------------------


def test_evaluating_without_an_adjustment_is_the_optimizers_plan(client):
    body = _manual_request()
    plain = client.post("/api/v1/optimize/", json=body).json()["data"]

    resp = _evaluate(client, body)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["layouts"] == plain["layouts"]
    assert data["optimizationHash"] == plain["optimizationHash"]
    assert data["adjustmentSummary"] is None and data["layoutIssues"] == []
    [pool] = data["pools"]
    assert pool["poolKey"] == "m1" and pool["pending"] == [] and not pool["adjusted"]
    assert [sheet["layout"] for sheet in pool["sheets"]] == plain["layouts"]
    assert {p["pieceId"] for p in pool["pieces"]} == {"P#1", "P#2", "P#3", "P#4"}
    assert pool["bins"] == [
        {
            "materialKey": "m1",
            "halfBoard": False,
            "width": 1000.0,
            "height": 1000.0,
            "costPerUnit": 40.0,
            "label": "1000×1000",
            "remaining": None,
        }
    ]


def test_a_piece_moved_to_a_new_sheet_costs_a_board(client):
    body = _manual_request()
    base = client.post("/api/v1/optimize/", json=body).json()["data"]
    adjustments = _move_to_new_sheet(client, body)

    edited = _evaluate(client, {**body, "layoutAdjustments": adjustments}).json()[
        "data"
    ]
    read = client.post(
        "/api/v1/optimize/", json={**body, "layoutAdjustments": adjustments}
    ).json()["data"]

    for data in (edited, read):
        assert data["totalBoardsUsed"] == base["totalBoardsUsed"] + 1
        assert data["adjustmentSummary"] == {
            "movedPieces": 1,
            "boardsDelta": 1,
            "boardCostDelta": 40.0,
            "wholeOffcuts": 0,
        }
        assert data["optimizationHash"] != base["optimizationHash"]
    moved = [
        p
        for layout in read["layouts"]
        for p in layout["placedPieces"]
        if p.get("adjusted")
    ]
    assert [p["pieceId"] for p in moved] == ["P#4"]
    assert read["layoutAdjustments"][0]["poolKey"] == "m1"
    # Untouched pieces carry no flag at all: the keys are quiet while false.
    untouched = read["layouts"][0]["placedPieces"][0]
    assert "adjusted" not in untouched


def test_the_editor_keeps_pending_pieces_and_says_which(client):
    body = _manual_request()
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    sheets = _working_sheets(pool)
    sheets[0]["pieces"] = [p for p in sheets[0]["pieces"] if p["pieceId"] != "P#2"]

    data = _evaluate(
        client, {**body, "layoutAdjustments": [{"poolKey": "m1", "sheets": sheets}]}
    ).json()["data"]

    assert data["pools"][0]["pending"] == ["P#2"]
    [unplaced] = data["unplaced"]
    # The useful area depends on the trims of the environment the suite runs in.
    unplaced.pop("usableHeight")
    unplaced.pop("usableWidth")
    assert unplaced == {
        "materialKey": "m1",
        "label": "P",
        "height": 450.0,
        "width": 450.0,
        "quantity": 1,
        "materialName": "tablero 1000×1000 mm",
        # It fits: the adjustment left it out, nothing ran out.
        "reason": "pending",
    }


def test_the_editor_refuses_a_sheet_that_cannot_be_cut(client):
    body = _manual_request()
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    sheets = _working_sheets(pool)
    first, second = sheets[0]["pieces"][:2]
    second.update(x=first["x"] + 100, y=first["y"])

    resp = _evaluate(
        client, {**body, "layoutAdjustments": [{"poolKey": "m1", "sheets": sheets}]}
    )

    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["field"] == "layoutAdjustments"
    assert "se cruza con" in error["message"]


def test_a_read_falls_back_to_the_optimizer_when_an_adjustment_does_not_hold(client):
    body = _manual_request()
    plain = client.post("/api/v1/optimize/", json=body).json()["data"]
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    sheets = _working_sheets(pool)
    sheets[0]["pieces"] = sheets[0]["pieces"][:3]  # one piece left pending

    data = client.post(
        "/api/v1/optimize/",
        json={**body, "layoutAdjustments": [{"poolKey": "m1", "sheets": sheets}]},
    ).json()["data"]

    assert data["layouts"] == plain["layouts"]
    assert data["optimizationHash"] == plain["optimizationHash"]
    assert [i["code"] for i in data["layoutIssues"]] == ["pending_pieces"]
    assert data["layoutAdjustments"] is None


def test_candidates_for_a_material_not_in_the_quote_are_refused(client):
    resp = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **_manual_request(),
            "probe": {"kind": "piece", "poolKey": "nope", "pieceId": "P#1"},
        },
    )

    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "probe.poolKey"


def test_candidates_across_sheets_say_where_a_piece_fits(client):
    body = _manual_request()
    adjustments = _move_to_new_sheet(client, body)

    resp = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **body,
            "layoutAdjustments": adjustments,
            "probe": {"kind": "piece", "poolKey": "m1", "pieceId": "P#1"},
        },
    )

    fits = resp.json()["data"]["sheets"]
    # The piece's own sheet always takes it back; the other has room for it.
    assert [(f["sheetIndex"], f["fits"]) for f in fits] == [(0, True), (1, True)]
    assert all(f["positions"] == [] for f in fits)


def _catalog_request(board_id, height=300, width=300, quantity=2):
    return {
        "materials": [{"key": "b1", "source": "catalog", "productId": board_id}],
        "requirements": [
            {
                "priority": 0,
                "height": height,
                "width": width,
                "quantity": quantity,
                "materialKey": "b1",
                "label": "Tapa",
            }
        ],
    }


def test_a_half_board_can_be_taken_as_a_whole_one(client):
    board = _create_board(client)
    body = _catalog_request(board["id"])
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    assert pool["sheets"][0]["halfBoard"] is True  # two small pieces: a half board

    conversions = client.post(
        "/api/v1/optimize/layout/candidates",
        json={**body, "probe": {"kind": "sheet", "poolKey": "b1", "sheetIndex": 0}},
    ).json()["data"]["conversions"]
    assert conversions == [{"halfBoard": False, "shiftX": 0.0, "shiftY": 0.0}]

    sheets = _working_sheets(pool)
    sheets[0]["halfBoard"] = False
    data = _evaluate(
        client, {**body, "layoutAdjustments": [{"poolKey": "b1", "sheets": sheets}]}
    ).json()["data"]
    assert data["totalBoardsUsed"] == 1
    assert data["layouts"][0]["material"]["halfBoard"] is False
    assert data["adjustmentSummary"]["boardCostDelta"] > 0


def test_an_offcut_can_be_grown_and_kept_whole(client):
    body = _manual_request(quantity=1)
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    layout = pool["sheets"][0]["layout"]
    small = min(layout["remainders"], key=lambda r: r["width"] * r["height"])

    extensions = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **body,
            "probe": {"kind": "leftover", "poolKey": "m1", "sheetIndex": 0, **small},
        },
    ).json()["data"]["extensions"]
    assert extensions, "the small offcut beside a lone piece can always grow"

    grown = {k: extensions[0][k] for k in ("x", "y", "width", "height")}
    sheets = _working_sheets(pool)
    sheets[0]["wholeOffcuts"] = [grown]
    data = _evaluate(
        client, {**body, "layoutAdjustments": [{"poolKey": "m1", "sheets": sheets}]}
    ).json()["data"]

    whole = [r for r in data["layouts"][0]["remainders"] if r.get("keptWhole")]
    assert [{k: r[k] for k in grown} for r in whole] == [grown]
    assert data["adjustmentSummary"]["wholeOffcuts"] == 1
    assert data["adjustmentSummary"]["movedPieces"] == 0


def test_a_piece_can_be_placed_on_an_offcut_that_was_grown(client):
    """Grow an offcut to make room, then use the room: the position says which
    whole offcut it uses, and the sheet holds without it."""
    body = _manual_request(quantity=1)
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    layout = pool["sheets"][0]["layout"]
    small = min(layout["remainders"], key=lambda r: r["width"] * r["height"])
    grown = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **body,
            "probe": {"kind": "leftover", "poolKey": "m1", "sheetIndex": 0, **small},
        },
    ).json()["data"]["extensions"][0]
    sheets = _working_sheets(pool)
    sheets[0]["wholeOffcuts"] = [{k: grown[k] for k in ("x", "y", "width", "height")}]
    # A second piece to place: with two instances the ids gain their `#N`, and the
    # new one starts pending.
    body2 = _manual_request(quantity=2)
    sheets[0]["pieces"][0]["pieceId"] = "P#1"
    adjustments = [{"poolKey": "m1", "sheets": sheets}]

    fits = client.post(
        "/api/v1/optimize/layout/candidates",
        json={
            **body2,
            "layoutAdjustments": adjustments,
            "probe": {
                "kind": "piece",
                "poolKey": "m1",
                "pieceId": "P#2",
                "sheetIndex": 0,
            },
        },
    ).json()["data"]["sheets"][0]["positions"]
    on_whole = [p for p in fits if p["usesWholeOffcuts"] == [0]]
    assert on_whole, "the grown offcut is room a piece can use"

    spot = on_whole[0]
    sheets[0]["pieces"].append(
        {"pieceId": "P#2", "x": spot["x"], "y": spot["y"], "rotated": spot["rotated"]}
    )
    sheets[0]["wholeOffcuts"] = []
    resp = _evaluate(client, {**body2, "layoutAdjustments": adjustments})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Pre-order -> order
# ---------------------------------------------------------------------------


def _preorder_body(client_id, body, adjustments=None):
    return {
        **body,
        "clientId": client_id,
        "branchId": _BRANCH,
        **({"layoutAdjustments": adjustments} if adjustments is not None else {}),
    }


def test_a_quote_keeps_its_adjustment_and_its_order_freezes_it(client, db_session):
    c = _create_client(client)
    body = _manual_request()
    adjustments = _move_to_new_sheet(client, body)

    created = client.post(
        "/api/v1/preorders/", json=_preorder_body(c["id"], body, adjustments)
    )
    assert created.status_code == 201, created.text
    detail = client.get(f"/api/v1/preorders/{created.json()['data']['id']}").json()[
        "data"
    ]

    assert detail["layoutAdjustments"][0]["poolKey"] == "m1"
    assert detail["optimization"]["totalBoardsUsed"] == 2
    assert detail["optimization"]["adjustmentSummary"]["movedPieces"] == 1

    link = client.post(f"/api/v1/preorders/{detail['id']}/review-link").json()["data"]
    confirmed = client.post(f"/api/v1/public/review/{link['token']}/confirm")
    assert confirmed.status_code == 200, confirmed.text

    order_id = client.get(f"/api/v1/preorders/{detail['id']}").json()["data"]["orderId"]
    order = db_session.get(OrderModel, order_id)
    assert order.total_boards_used == 2
    assert len(order.boards) == 2
    assert order.optimization_snapshot["layout_adjustments"][0]["pool_key"] == "m1"


def test_a_quote_refuses_an_adjustment_that_does_not_hold(client):
    c = _create_client(client)
    body = _manual_request()
    pool = _evaluate(client, body).json()["data"]["pools"][0]
    sheets = _working_sheets(pool)
    sheets[0]["pieces"] = sheets[0]["pieces"][:3]

    resp = client.post(
        "/api/v1/preorders/",
        json=_preorder_body(c["id"], body, [{"poolKey": "m1", "sheets": sheets}]),
    )

    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "layoutAdjustments"
    assert "sin ubicar" in resp.json()["errors"][0]["message"]


def test_changing_the_cut_list_drops_the_adjustment_it_broke(client):
    c = _create_client(client)
    body = _manual_request()
    adjustments = _move_to_new_sheet(client, body)
    pre = client.post(
        "/api/v1/preorders/", json=_preorder_body(c["id"], body, adjustments)
    ).json()["data"]

    # A fifth piece is not on any adjusted sheet: the pool no longer holds.
    grown = _manual_request(quantity=5)
    resp = client.put(
        f"/api/v1/preorders/{pre['id']}", json={"requirements": grown["requirements"]}
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["layoutAdjustments"] is None
    assert data["optimization"]["adjustmentSummary"] is None


def test_an_edit_elsewhere_keeps_the_adjustment_and_null_clears_it(client):
    c = _create_client(client)
    body = _manual_request()
    adjustments = _move_to_new_sheet(client, body)
    pre = client.post(
        "/api/v1/preorders/", json=_preorder_body(c["id"], body, adjustments)
    ).json()["data"]

    kept = client.put(
        f"/api/v1/preorders/{pre['id']}", json={"notes": "Cocina"}
    ).json()["data"]
    assert kept["layoutAdjustments"] is not None

    cleared = client.put(
        f"/api/v1/preorders/{pre['id']}", json={"layoutAdjustments": None}
    ).json()["data"]
    assert cleared["layoutAdjustments"] is None
    assert cleared["optimization"]["totalBoardsUsed"] == 1


def test_two_orders_that_differ_only_by_an_adjustment_are_not_deduplicated(
    client, db_session
):
    c = _create_client(client)
    body = _manual_request()
    adjustments = _move_to_new_sheet(client, body)
    service = OrderService(db_session)
    payload = {**body, "clientId": c["id"], "branchId": _BRANCH}

    plain = service.create(OrderCreate.model_validate(payload))
    adjusted = service.create(
        OrderCreate.model_validate({**payload, "layoutAdjustments": adjustments})
    )
    again = service.create(
        OrderCreate.model_validate({**payload, "layoutAdjustments": adjustments})
    )

    assert adjusted.id != plain.id
    assert again.id == adjusted.id
    assert (plain.total_boards_used, adjusted.total_boards_used) == (1, 2)
