"""Pausing an activity that cannot go on: no boards, no edge banding, a machine
down, or the station before not handing over more pieces.

A pause is a property of work under way -- the activity keeps its
``in_progress`` status and carries ``pausedAt``/``pauseReason`` -- and what it
buys is the stretch out of the worker's time. The time itself is covered in
``tests/unit/test_production.py`` and ``test_analytics_performance.py``; here it
is the gates, the record and who gets told. Which reason stops which activity
is ``tests/unit/test_order_pauses.py``; here it is that the API holds to it.
"""

from datetime import datetime

from src.modules.orders.model import OrderActivityModel, OrderActivityPauseModel
from tests.order_helpers import (
    _activity,
    _banded_pieces,
    _create_board,
    _create_client,
    _create_edge_banding,
    _cut_all_pieces,
    _cut_first_banded_piece,
    _cut_piece,
    _mint_order,
    _order_mixed_pieces,
    _order_with_banding,
    _order_with_services,
    _order_without_banding,
    _patch_activity,
    _patch_status,
    _to_in_process,
    _token_for,
    _worked_pieces,
)


def _pause(client, oid, activity="cutting", reason="no_boards", note=None, **kw):
    body = {"reason": reason}
    if note is not None:
        body["note"] = note
    return client.post(
        f"/api/v1/orders/{oid}/activities/{activity}/pause", json=body, **kw
    )


def _resume(client, oid, activity="cutting", **kw):
    return client.post(f"/api/v1/orders/{oid}/activities/{activity}/resume", **kw)


def _pieces(client, oid):
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    return [piece for board in plan["boards"] for piece in board["pieces"]]


def _message(resp):
    return resp.json()["errors"][0]["message"]


def _refused_reason(resp):
    """Whether the pause bounced on its ``reason``, the field the dialog marks."""
    return resp.status_code == 422 and resp.json()["errors"][0]["field"] == "reason"


def _plain_pieces(client, oid):
    """Placed pieces with no edge banding, in cutting-plan order."""
    return [p for p in _pieces(client, oid) if not p["edges"]]


def _board_card(client, oid):
    board = client.get("/api/v1/orders/workshop-queue").json()["data"]
    return next(item for item in board if item["orderId"] == oid)


def _history_notes(client, oid):
    order = client.get(f"/api/v1/orders/{oid}").json()["data"]
    return [row["note"] for row in order["history"]]


def _banding_under_way(client, db_session):
    """Two banded pieces and two plain ones; one banded cut and the banding begun."""
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )
    return order


def _order_with_banded_doors(client, db_session, identifier="0100000397"):
    """Two pieces that are banded AND carry a workshop code: they cross the edge
    bander before anybody hinges them."""
    c = _create_client(client, identifier=identifier)
    suffix = identifier[-4:]
    b = _create_board(client, code=f"MEL{suffix}")
    eb = _create_edge_banding(client, code=f"TAP{suffix}")
    return _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": 1,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [
                {
                    "priority": 0,
                    "height": 500,
                    "width": 1000,
                    "quantity": 2,
                    "materialKey": "b1",
                    "label": "Puerta",
                    "canRotate": True,
                    "edgeBanding": {"productId": eb["id"], "sides": ["top", "bottom"]},
                    "hingingCode": "B2",
                }
            ],
        },
    )


# ------------------------------------------------------------------ the pause
def test_pausing_the_cut_keeps_it_in_progress_and_says_why(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])

    resp = _pause(client, order["id"], note="Faltan 2 planchas de RATÓN")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["orderStatus"] == "in_process"
    cut = data["activity"]
    assert cut["status"] == "in_progress"
    assert cut["pausedAt"] is not None
    assert cut["pauseReason"] == "no_boards"
    [pause] = cut["pauses"]
    assert (pause["reason"], pause["endedAt"]) == ("no_boards", None)
    assert pause["note"] == "Faltan 2 planchas de RATÓN"
    assert pause["startedByLabel"] == "Conftest Admin"


def test_the_pause_shows_on_the_order_s_timeline_without_moving_its_clock(
    client, db_session
):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    before = client.get(f"/api/v1/orders/{order['id']}").json()["data"]

    _pause(client, order["id"], reason="machine_failure", note="Se rompió la correa")
    after = client.get(f"/api/v1/orders/{order['id']}").json()["data"]

    row = after["history"][-1]
    assert row["fromStatus"] == row["toStatus"] == "in_process"
    assert row["note"] == "Corte en pausa: Daño de máquina — Se rompió la correa"
    # Not a transition: the status clock is the one of taking the order.
    assert after["statusChangedAt"] == before["statusChangedAt"]
    assert _activity(after, "cutting")["pauseReason"] == "machine_failure"


def test_resuming_closes_the_pause(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _pause(client, order["id"])

    resp = _resume(client, order["id"])

    assert resp.status_code == 200
    cut = resp.json()["data"]["activity"]
    assert cut["pausedAt"] is None and cut["pauseReason"] is None
    [pause] = cut["pauses"]
    assert pause["endedAt"] is not None
    assert pause["endedByLabel"] == "Conftest Admin"
    history = client.get(f"/api/v1/orders/{order['id']}").json()["data"]["history"]
    assert history[-1]["note"] == "Corte reanudado"


def test_pausing_and_resuming_are_idempotent(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])

    assert _resume(client, order["id"]).status_code == 200  # nothing to resume
    first = _pause(client, order["id"]).json()["data"]["activity"]
    # A second tap, even with another reason, changes nothing.
    again = _pause(client, order["id"], reason="machine_failure").json()["data"]
    assert again["activity"]["pauseReason"] == "no_boards"
    assert again["activity"]["pausedAt"] == first["pausedAt"]
    assert len(again["activity"]["pauses"]) == 1

    _resume(client, order["id"])
    assert len(_resume(client, order["id"]).json()["data"]["activity"]["pauses"]) == 1


def test_an_activity_can_be_paused_more_than_once(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _pause(client, order["id"])
    _resume(client, order["id"])

    cut = _pause(client, order["id"], reason="machine_failure").json()["data"]

    reasons = [p["reason"] for p in cut["activity"]["pauses"]]
    assert reasons == ["no_boards", "machine_failure"]


# ------------------------------------------------------------------ the gates
def test_a_paused_cut_takes_no_marks(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    piece = _pieces(client, order["id"])[0]
    _pause(client, order["id"])

    blocked = _cut_piece(client, order["id"], piece)
    assert blocked.status_code == 422
    assert "en pausa" in _message(blocked)

    _resume(client, order["id"])
    assert _cut_piece(client, order["id"], piece).status_code == 200


def test_a_paused_activity_cannot_be_finished(client, db_session):
    """Closing straight out of a pause would leave the work since out of its time."""
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    _pause(client, order["id"], reason="machine_failure")

    blocked = _patch_activity(client, order["id"], "cutting", "done")
    assert blocked.status_code == 422
    assert "reanúdalo" in _message(blocked)

    _resume(client, order["id"])
    assert _patch_activity(client, order["id"], "cutting", "done").status_code == 200


def test_only_work_under_way_can_be_paused(client, db_session):
    order = _order_with_banding(client, db_session)
    # Still in the queue: nothing has started.
    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _pause(client, order["id"]).status_code == 422

    _patch_activity(client, order["id"], "cutting", "in_progress")
    # The banding has not started either: pending is not paused, it is waiting.
    pending = _pause(client, order["id"], "banding", "no_edge_banding")
    assert pending.status_code == 422
    assert "en curso" in _message(pending)


def test_an_order_without_the_activity_has_nothing_to_pause(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    resp = _pause(client, order["id"], "banding", "no_edge_banding")
    assert resp.status_code == 422
    assert "no lleva" in _message(resp)


def test_an_unknown_reason_is_rejected(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    assert _pause(client, order["id"], reason="almuerzo").status_code == 422


def test_each_role_pauses_only_its_own_activities(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    _patch_activity(client, order["id"], "banding", "in_progress")
    operator = _token_for(client, db_session, "operador")
    bander = _token_for(client, db_session, "canteador")
    seller = _token_for(client, db_session, "vendedor")

    # The seller has no hand on the shop floor's work at all.
    assert _pause(client, order["id"], headers=seller).status_code == 403
    assert _pause(client, order["id"], headers=bander).status_code == 403
    assert (
        _pause(
            client, order["id"], "banding", "no_edge_banding", headers=operator
        ).status_code
        == 403
    )
    assert _pause(client, order["id"], headers=operator).status_code == 200
    assert (
        _pause(
            client, order["id"], "banding", "machine_failure", headers=bander
        ).status_code
        == 200
    )
    # And only who may register the activity lifts its pause.
    assert _resume(client, order["id"], headers=bander).status_code == 403
    assert _resume(client, order["id"], headers=operator).status_code == 200


def test_the_rollback_to_the_queue_lifts_the_cut_s_pause(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _pause(client, order["id"])

    assert _patch_status(client, order["id"], "queued").status_code == 200

    cut = _activity(
        client.get(f"/api/v1/orders/{order['id']}").json()["data"], "cutting"
    )
    assert cut["status"] == "pending"
    assert cut["pausedAt"] is None and cut["pauseReason"] is None
    assert cut["pauses"][0]["endedAt"] is not None


# ------------------------------------------------- what can stop each activity
def test_order_232_a_banding_with_everything_cut_is_not_out_of_boards(
    client, db_session
):
    """The cut closed the day before; the banding cannot stop for boards, nor
    wait for pieces that are all already off the saw."""
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_activity(client, order["id"], "cutting", "done").status_code == 200
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )

    no_boards = _pause(client, order["id"], "banding", "no_boards")
    assert _refused_reason(no_boards)
    assert _message(no_boards) == "«Sin tableros» no es un motivo de pausa de canteado"
    waiting = _pause(client, order["id"], "banding", "waiting_for_pieces")
    assert _refused_reason(waiting)
    assert "todas las piezas con canto" in _message(waiting)
    # Nothing was written by the two refusals.
    banding = _activity(_board_card(client, order["id"]), "banding")
    assert banding["pausedAt"] is None and banding["pauses"] == []
    assert banding["pauseReasons"] == ["no_edge_banding", "machine_failure"]

    assert _pause(client, order["id"], "banding", "no_edge_banding").status_code == 200


def test_the_saw_uses_no_tape_and_waits_for_nobody(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])

    assert _refused_reason(_pause(client, order["id"], reason="no_edge_banding"))
    assert _refused_reason(_pause(client, order["id"], reason="waiting_for_pieces"))
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    assert _activity(plan, "cutting")["pauseReasons"] == [
        "no_boards",
        "machine_failure",
    ]


def test_the_bander_waits_for_pieces_while_banded_ones_are_on_the_saw(
    client, db_session
):
    order = _banding_under_way(client, db_session)
    card = _activity(_board_card(client, order["id"]), "banding")
    assert card["pauseReasons"] == [
        "waiting_for_pieces",
        "no_edge_banding",
        "machine_failure",
    ]

    resp = _pause(client, order["id"], "banding", "waiting_for_pieces")

    assert resp.status_code == 200, resp.text
    banding = resp.json()["data"]["activity"]
    assert banding["pauseReason"] == "waiting_for_pieces"
    # What the saw had released when the bander said so: 1 of the 2 banded.
    assert _history_notes(client, order["id"])[-1] == (
        "Canteado en pausa: Esperando piezas (1 de 2 con canto cortadas)"
    )


def test_a_wait_lifts_itself_when_a_piece_of_its_set_is_cut(client, db_session):
    order = _banding_under_way(client, db_session)
    oid = order["id"]
    _pause(client, oid, "banding", "waiting_for_pieces")

    # A plain piece is not what the bander waits for.
    plain = _plain_pieces(client, oid)[0]
    assert _cut_piece(client, oid, plain).status_code == 200
    assert _activity(_board_card(client, oid), "banding")["pausedAt"] is not None

    banded = _banded_pieces(client, oid)[1]
    marked = _cut_piece(client, oid, banded).json()["data"]["piece"]

    banding = _activity(_board_card(client, oid), "banding")
    assert banding["pausedAt"] is None and banding["pauseReason"] is None
    [pause] = banding["pauses"]
    # One instant: the wait ends at the mark, not at whenever somebody noticed.
    assert pause["endedAt"] == marked["cutAt"]
    assert pause["endedByLabel"] == "Conftest Admin"
    assert _history_notes(client, oid)[-1] == "Canteado reanudado: llegaron piezas"
    # Every banded piece is cut now, so it closes without a tap on «Reanudar».
    assert _patch_activity(client, oid, "banding", "done").status_code == 200


def test_unmarking_the_piece_does_not_bring_the_wait_back(client, db_session):
    order = _banding_under_way(client, db_session)
    oid = order["id"]
    _pause(client, oid, "banding", "waiting_for_pieces")
    banded = _banded_pieces(client, oid)[1]
    _cut_piece(client, oid, banded)

    assert _cut_piece(client, oid, banded, cut=False).status_code == 200

    banding = _activity(_board_card(client, oid), "banding")
    assert banding["pausedAt"] is None
    assert len(banding["pauses"]) == 1
    # It is owed that piece again, so it may wait again.
    assert "waiting_for_pieces" in banding["pauseReasons"]


def test_a_piece_off_the_saw_does_not_lift_any_other_pause(client, db_session):
    """A piece arriving does not bring tape, nor mend a machine."""
    order = _banding_under_way(client, db_session)
    oid = order["id"]
    _pause(client, oid, "banding", "no_edge_banding")

    _cut_piece(client, oid, _banded_pieces(client, oid)[1])

    banding = _activity(_board_card(client, oid), "banding")
    assert banding["pauseReason"] == "no_edge_banding"


def test_waiting_for_pieces_tells_nobody(client, db_session):
    """The line's own rhythm: nobody in the office can hurry the saw."""
    order = _banding_under_way(client, db_session)
    bander = _token_for(client, db_session, "canteador")
    other_admin = _token_for(
        client, db_session, "administrador", email="otra@empresa.com"
    )

    resp = _pause(client, order["id"], "banding", "waiting_for_pieces", headers=bander)

    assert resp.status_code == 200
    items = client.get(
        "/api/v1/notifications/", headers=other_admin, params={"unread": "true"}
    ).json()["data"]
    assert [n for n in items if n["type"] == "order.activity_paused"] == []


def test_the_additional_work_waits_on_the_banding_and_its_close_lifts_it(
    client, db_session
):
    order = _order_with_services(client, db_session)
    oid = order["id"]
    _to_in_process(client, oid)
    _cut_all_pieces(client, oid)
    for activity in ("banding", "additional"):
        assert _patch_activity(client, oid, activity, "in_progress").status_code == 200
    # Every worked piece is cut, but the order's banding is still open.
    extra = _activity(_board_card(client, oid), "additional")
    assert extra["pauseReasons"] == ["waiting_for_pieces", "machine_failure"]
    assert _refused_reason(_pause(client, oid, "additional", "no_edge_banding"))
    assert _refused_reason(_pause(client, oid, "additional", "no_boards"))
    assert _pause(client, oid, "additional", "waiting_for_pieces").status_code == 200

    assert _patch_activity(client, oid, "banding", "done").status_code == 200

    extra = _activity(_board_card(client, oid), "additional")
    assert extra["pausedAt"] is None
    assert extra["pauses"][0]["endedAt"] is not None
    assert "Adicionales reanudado: llegaron piezas" in _history_notes(client, oid)
    # Cut and banded: nothing is owed to it any more.
    assert extra["pauseReasons"] == ["machine_failure"]
    assert _refused_reason(_pause(client, oid, "additional", "waiting_for_pieces"))


def test_a_worked_piece_with_no_banding_reaches_the_additional_work_when_cut(
    client, db_session
):
    order = _order_with_services(client, db_session)
    oid = order["id"]
    _to_in_process(client, oid)
    first, second = _worked_pieces(client, oid)
    _cut_piece(client, oid, first)
    assert _patch_activity(client, oid, "additional", "in_progress").status_code == 200
    _pause(client, oid, "additional", "waiting_for_pieces")

    _cut_piece(client, oid, second)

    assert _activity(_board_card(client, oid), "additional")["pausedAt"] is None


def test_a_worked_piece_that_is_banded_arrives_only_when_the_banding_closes(
    client, db_session
):
    order = _order_with_banded_doors(client, db_session)
    oid = order["id"]
    _to_in_process(client, oid)
    first, second = _worked_pieces(client, oid)
    _cut_piece(client, oid, first)
    for activity in ("banding", "additional"):
        assert _patch_activity(client, oid, activity, "in_progress").status_code == 200
    _pause(client, oid, "additional", "waiting_for_pieces")

    # Off the saw, but on its way to the edge bander: not here yet.
    _cut_piece(client, oid, second)
    assert _activity(_board_card(client, oid), "additional")["pausedAt"] is not None

    assert _patch_activity(client, oid, "banding", "done").status_code == 200
    assert _activity(_board_card(client, oid), "additional")["pausedAt"] is None


def test_the_order_detail_does_not_offer_reasons(client, db_session):
    """Its rows come straight from the table, with no counts to gate on."""
    order = _banding_under_way(client, db_session)
    detail = client.get(f"/api/v1/orders/{order['id']}").json()["data"]
    assert _activity(detail, "banding")["pauseReasons"] is None


def test_a_pause_saved_under_the_old_rule_still_reads_and_resumes(client, db_session):
    """Any reason used to fit any activity. Those rows are history, not errors."""
    order = _order_with_banding(client, db_session)
    oid = order["id"]
    _to_in_process(client, oid)
    _cut_all_pieces(client, oid)
    _patch_activity(client, oid, "banding", "in_progress")
    banding = (
        db_session.query(OrderActivityModel).filter_by(order_id=oid, type="banding")
    ).one()
    at = datetime.utcnow()
    banding.pauses.append(
        OrderActivityPauseModel(order_id=oid, reason="no_boards", started_at=at)
    )
    banding.paused_at = at
    banding.pause_reason = "no_boards"
    db_session.commit()

    detail = client.get(f"/api/v1/orders/{oid}").json()["data"]
    assert _activity(detail, "banding")["pauseReason"] == "no_boards"
    card = _activity(_board_card(client, oid), "banding")
    assert card["pauseReason"] == "no_boards"
    assert card["pauses"][0]["reason"] == "no_boards"

    assert _resume(client, oid, "banding").status_code == 200
    assert _activity(_board_card(client, oid), "banding")["pausedAt"] is None


# ---------------------------------------------------------------- who sees it
def test_the_board_and_the_listing_show_the_pause(client, db_session):
    paused = _order_without_banding(client, db_session)
    _to_in_process(client, paused["id"])
    _pause(client, paused["id"], reason="machine_failure")
    running = _order_without_banding(client, db_session, identifier="0100000900")
    _to_in_process(client, running["id"])

    board = client.get("/api/v1/orders/workshop-queue").json()["data"]
    cards = {item["orderId"]: _activity(item, "cutting") for item in board}
    assert cards[paused["id"]]["pauseReason"] == "machine_failure"
    assert cards[running["id"]]["pausedAt"] is None

    def listed(**params):
        items = client.get("/api/v1/orders/", params=params).json()["data"]
        return {item["id"] for item in items}

    assert listed(paused="true") == {paused["id"]}
    assert listed(paused="false") == {running["id"]}
    assert listed(paused="true", activity="banding") == set()


def test_a_quiet_saw_with_its_only_cut_paused_says_paused(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _pause(client, order["id"], reason="machine_failure")
    code = client.get(f"/api/v1/orders/{order['id']}").json()["data"]["code"]

    # The take itself is a cutting event within the idle gap: still "cutting".
    live = client.get("/api/v1/orders/production-status").json()["data"]
    [branch] = [b for b in live["branches"] if b["branchId"] == 1]
    assert branch["pausedOrderCodes"] == [code]
    assert branch["cuttingOrderCodes"] == []


def test_pausing_tells_the_office_and_not_who_paused(client, db_session):
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    operator = _token_for(client, db_session, "operador")
    other_admin = _token_for(
        client, db_session, "administrador", email="otra@empresa.com"
    )

    assert (
        _pause(
            client, order["id"], note="No llegó el RATÓN", headers=operator
        ).status_code
        == 200
    )

    def unread(headers):
        items = client.get(
            "/api/v1/notifications/", headers=headers, params={"unread": "true"}
        ).json()["data"]
        return [n for n in items if n["type"] == "order.activity_paused"]

    [note] = unread(other_admin)
    assert "corte en pausa" in note["title"]
    assert note["body"].startswith("Sin tableros.")
    assert "No llegó el RATÓN" in note["body"]
    assert note["data"]["reason"] == "no_boards"
    # The operator paused it: nobody is told about their own tap.
    assert unread(operator) == []
    # Resuming asks nothing of anybody.
    _resume(client, order["id"], headers=operator)
    assert len(unread(other_admin)) == 1
