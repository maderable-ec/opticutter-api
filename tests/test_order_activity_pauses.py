"""Pausing an activity that cannot go on: no boards, no edge banding, a machine down.

A pause is a property of work under way -- the activity keeps its
``in_progress`` status and carries ``pausedAt``/``pauseReason`` -- and what it
buys is the stretch out of the worker's time. The time itself is covered in
``tests/unit/test_production.py`` and ``test_analytics_performance.py``; here it
is the gates, the record and who gets told.
"""

from tests.order_helpers import (
    _activity,
    _cut_all_pieces,
    _cut_first_banded_piece,
    _cut_piece,
    _order_with_banding,
    _order_without_banding,
    _patch_activity,
    _patch_status,
    _to_in_process,
    _token_for,
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
    # Any reason on any activity: the bander runs out of pieces with the saw.
    assert (
        _pause(client, order["id"], "banding", "no_boards", headers=bander).status_code
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
