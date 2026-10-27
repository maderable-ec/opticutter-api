"""Ensamble: the layers of an assembly are cut alike and large.

The seller types one row per layer with the same Ens code and the FINAL size;
the canto goes on one of them. These tests hold the rule end to end: both
layers come out of the saw at the same size even though only one carries the
hard tape, the canto is billed once, the order and its export cut what the plan
cut, a write refuses an assembly whose layers disagree, and a quote saved before
the rule (a code with no mark) is untouched. The millimetres are settings
(5 large and 1 short per hard side by default), and an order keeps the ones its
plan was cut with.
"""

import csv
import io

from src.modules.preorders.model import PreOrderModel
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.security import create_access_token
from tests.order_helpers import _mint_order
from tests.test_hard_edge_cut import _setup


def _layer(height=600, width=400, code="E1", mark=True, tape=None, **extra):
    row = {
        "priority": 0,
        "height": height,
        "width": width,
        "quantity": 3,
        "materialKey": extra.pop("materialKey", "b1"),
        "label": "Tapa",
        # Not rotatable, so the placed size reads in the piece's own frame.
        "canRotate": False,
        "assemblyCode": code,
        **({"assemblyAllowance": True} if mark else {}),
        **extra,
    }
    if tape is not None:
        row["edgeBanding"] = {"productId": tape, "sides": ["left", "right"]}
    return row


def _body(client_id, board_id, *rows):
    return {
        "clientId": client_id,
        "branchId": 1,
        "materials": [{"key": "b1", "source": "catalog", "productId": board_id}],
        "requirements": list(rows),
    }


def _optimize(client, body):
    resp = client.post("/api/v1/optimize/", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _sizes(data):
    """``(label-free) [(height, width, originalHeight, originalWidth)]`` placed."""
    return sorted(
        (p["height"], p["width"], p["originalHeight"], p["originalWidth"])
        for layout in data["layouts"]
        for p in layout["placedPieces"]
    )


def test_both_layers_are_cut_alike_and_the_canto_is_billed_once(client):
    client_id, board_id, hard, _ = _setup(client)
    body = _body(client_id, board_id, _layer(tape=hard), _layer())

    data = _optimize(client, body)

    # 2L CD on the first layer: +5 on each measure, 2 mm back off the ANCHO of
    # BOTH layers. Six pieces, all 605×403, all ordered at 600×400.
    assert _sizes(data) == [(605, 403, 600, 400)] * 6
    # The canto is the assembled piece's: three pieces × two sides of 600 mm.
    assert data["edgeBandingsSummary"][0]["netLinearM"] == 3.6
    # The diagram reads the code and the final size off every layer -- the
    # pattern groups it draws included.
    codes = {
        p["assemblyCode"] for layout in data["layouts"] for p in layout["placedPieces"]
    }
    assert codes == {"E1"}
    drawn = [p for g in data["layoutGroups"] for p in g["layout"]["placedPieces"]]
    assert {p["assemblyCode"] for p in drawn} == {"E1"}
    assert {(p["originalHeight"], p["originalWidth"]) for p in drawn} == {(600, 400)}


def test_a_code_saved_before_the_rule_moves_nothing(client):
    client_id, board_id, hard, _ = _setup(client)
    legacy = _body(
        client_id,
        board_id,
        _layer(tape=hard, mark=False),
        _layer(mark=False),
    )
    plain = _body(
        client_id,
        board_id,
        _layer(tape=hard, code=None, mark=False),
        _layer(code=None, mark=False),
    )

    legacy_data = _optimize(client, legacy)

    assert _sizes(legacy_data) == sorted(
        [(600, 398, 600, 400)] * 3 + [(600, 400, 600, 400)] * 3
    )
    # Same hash as with no code at all: every quote saved before the rule keeps
    # its Redis entry and its plan.
    assert (
        legacy_data["optimizationHash"] == _optimize(client, plain)["optimizationHash"]
    )


def test_renaming_an_assembly_keeps_its_plan(client):
    client_id, board_id, hard, _ = _setup(client)
    e1 = _optimize(client, _body(client_id, board_id, _layer(tape=hard), _layer()))
    renamed = _optimize(
        client,
        _body(client_id, board_id, _layer(tape=hard, code="A"), _layer(code=" a ")),
    )

    assert renamed["optimizationHash"] == e1["optimizationHash"]


def _error(resp):
    assert resp.status_code == 422, resp.text
    [error] = resp.json()["errors"]
    return error


def test_a_quote_whose_layers_disagree_is_not_saved(client):
    client_id, board_id, hard, _ = _setup(client)
    body = _body(client_id, board_id, _layer(tape=hard), _layer(width=398))

    error = _error(client.post("/api/v1/preorders/", json=body))

    assert error["code"] == "ASSEMBLY_MISMATCH"
    assert (
        "Ens E1: las capas no tienen la misma medida (600×400 / 600×398)"
        in (error["message"])
    )
    assert client.get("/api/v1/preorders/").json()["meta"]["pagination"]["total"] == 0
    # The optimizer itself still answers: the seller is mid-edit there.
    assert _optimize(client, body)["layouts"]


def test_an_update_that_breaks_an_assembly_is_refused(client):
    client_id, board_id, hard, _ = _setup(client)
    pre = client.post(
        "/api/v1/preorders/",
        json=_body(client_id, board_id, _layer(tape=hard), _layer()),
    ).json()["data"]

    resp = client.put(
        f"/api/v1/preorders/{pre['id']}",
        json={"requirements": [_layer(tape=hard), _layer(tape=hard)]},
    )

    assert _error(resp)["message"].startswith(
        "Ens E1: el canto va en una sola capa y lo tienen 2"
    )


def test_the_client_confirming_a_broken_assembly_is_told_to_call_sales(
    client, db_session
):
    client_id, board_id, hard, _ = _setup(client)
    pre = client.post(
        "/api/v1/preorders/",
        json=_body(client_id, board_id, _layer(tape=hard), _layer()),
    ).json()["data"]
    link = client.post(f"/api/v1/preorders/{pre['id']}/review-link").json()["data"]
    # Broken behind the API's back, the way only a stored quote can be.
    db_pre = db_session.get(PreOrderModel, pre["id"])
    db_pre.requirements = [db_pre.requirements[0]]
    db_session.commit()

    error = _error(client.post(f"/api/v1/public/review/{link['token']}/confirm"))

    assert "contacta a ventas" in error["message"]
    assert "Ens" not in error["message"]


def test_the_order_freezes_the_mark_and_exports_the_assembly_cut(client, db_session):
    client_id, board_id, hard, _ = _setup(client)
    body = _body(client_id, board_id, _layer(tape=hard), _layer())
    order = _mint_order(client, db_session, body)

    # The cut list keeps the ordered size ...
    assert [(p["height"], p["width"]) for p in order["pieces"]] == [(600, 400)] * 2
    # ... the operator's board draws the cut and names the final size ...
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    placed = [p for board in plan["boards"] for p in board["pieces"]]
    assert {(p["height"], p["width"]) for p in placed} == {(605, 403)}
    assert {(p["originalHeight"], p["originalWidth"]) for p in placed} == {(600, 400)}
    # ... and the shop's cutting program gets the cut for BOTH layers.
    url = f"/api/v1/orders/{order['id']}/pieces/export"
    rows = list(
        csv.reader(
            io.StringIO(
                client.get(url, params={"format": "csv"}).content.decode("utf-8-sig")
            )
        )
    )
    assert [r[1:3] for r in rows[1:]] == [["605", "403"], ["605", "403"]]


def test_the_millimetres_are_the_settings_and_an_order_keeps_its_own(
    client, db_session
):
    client_id, board_id, hard, _ = _setup(client)
    body = _body(client_id, board_id, _layer(tape=hard), _layer())
    legacy = _body(client_id, board_id, _layer(code=None, mark=False))
    before, legacy_before = _optimize(client, body), _optimize(client, legacy)

    patched = client.patch(
        "/api/v1/settings/cutting",
        json={"hardEdgeCutMm": 2, "assemblyAllowanceMm": 3},
    )
    assert patched.status_code == 200, patched.text

    data = _optimize(client, body)
    # 600 + 3 on the largo; 400 + 3 - 2×2 on the ancho, both layers.
    assert _sizes(data) == [(603, 399, 600, 400)] * 6
    assert data["optimizationHash"] != before["optimizationHash"]
    # A quote neither rule touches keeps its hash (and its Redis entry).
    assert (
        _optimize(client, legacy)["optimizationHash"]
        == legacy_before["optimizationHash"]
    )

    order = _mint_order(client, db_session, body)
    url = f"/api/v1/orders/{order['id']}/pieces/export"

    def exported():
        resp = client.get(url, params={"format": "csv"})
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
        return [r[1:3] for r in rows[1:]]

    assert exported() == [["603", "399"], ["603", "399"]]
    # Back to the defaults: the order still cuts what its plan was cut with.
    client.patch(
        "/api/v1/settings/cutting",
        json={"hardEdgeCutMm": 1, "assemblyAllowanceMm": 5},
    )
    assert exported() == [["603", "399"], ["603", "399"]]


def test_the_seller_reads_the_millimetres_and_only_the_admin_sets_them(
    client, db_session
):
    user = UserService(db_session).create(
        UserCreate(
            email="seller-cut-rules@empresa.com",
            password="seller-password",
            roles=["vendedor"],
            full_name="Seller",
            branch_id=1,
        )
    )
    seller = {"Authorization": f"Bearer {create_access_token(user.id, user.roles)}"}
    resp = client.get("/api/v1/optimize/cut-rules", headers=seller)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"hardEdgeCutMm": 1, "assemblyAllowanceMm": 5}

    assert (
        client.patch(
            "/api/v1/settings/cutting", json={"hardEdgeCutMm": 2}, headers=seller
        ).status_code
        == 403
    )
    client.patch("/api/v1/settings/cutting", json={"assemblyAllowanceMm": 4})
    resp = client.get("/api/v1/optimize/cut-rules", headers=seller)
    assert resp.json()["data"] == {"hardEdgeCutMm": 1, "assemblyAllowanceMm": 4}
