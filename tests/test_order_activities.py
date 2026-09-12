"""Tests for the three PARALLEL activities of an order in process.

Replaces ``test_order_banding.py``: the banding is now one activity of three
(cut, banding, additional), the order's own status is DERIVED from them, and the
helpers that used to live here moved to ``tests/order_helpers.py`` -- five other
modules were importing them from a test file.

Covered here: which activities an order is born with, the derived transitions,
the piece floors (including the ones that keep the tracks parallel), the closing
gate, idempotency, the per-activity clocks and the shop-floor RBAC. The board
that lists these orders is covered in ``test_order_workshop_queue.py``.
"""

from tests.order_helpers import (
    _activity,
    _banded_pieces,
    _cut_all_pieces,
    _cut_banded_pieces,
    _cut_first_banded_piece,
    _cut_piece,
    _order_mixed_pieces,
    _order_with_banding,
    _order_with_services,
    _order_without_banding,
    _patch_activity,
    _patch_status,
    _to_in_process,
    _token_for,
)


def _order_row(client, oid):
    return client.get(f"/api/v1/orders/{oid}").json()["data"]


def _types(order):
    return sorted(a["type"] for a in order["activities"])


# --------------------------------------------------------------------------- #
# Which activities an order is born with
# --------------------------------------------------------------------------- #
def test_every_order_starts_with_the_cut_pending(client, db_session):
    order = _order_without_banding(client, db_session)
    assert _types(order) == ["cutting"]
    cut = _activity(order, "cutting")
    assert cut["status"] == "pending"
    assert cut["startedAt"] is None and cut["finishedAt"] is None


def test_edge_banding_adds_the_banding_activity(client, db_session):
    order = _order_with_banding(client, db_session)
    assert _types(order) == ["banding", "cutting"]
    assert _activity(order, "banding")["status"] == "pending"


def test_additional_services_add_the_additional_activity(client, db_session):
    """The activity exists because a service was registered, not because of geometry."""
    order = _order_with_services(client, db_session)
    assert _types(order) == ["additional", "banding", "cutting"]
    assert _activity(order, "additional")["status"] == "pending"


def test_an_order_without_services_has_no_additional_activity(client, db_session):
    """A missing row is the old ``not_applicable``: nothing to move, nothing to gate."""
    order = _order_with_banding(client, db_session)
    assert _activity(order, "additional") is None
    _to_in_process(client, order["id"])
    resp = _patch_activity(client, order["id"], "additional", "in_progress")
    assert resp.status_code == 422
    assert "adicionales" in resp.json()["errors"][0]["message"].lower()


# --------------------------------------------------------------------------- #
# The order's status is derived from the activities
# --------------------------------------------------------------------------- #
def test_starting_the_cut_takes_the_order_out_of_the_queue(client, db_session):
    """One act, not two: the operator starts cutting and the order moves itself."""
    order = _order_without_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200

    started = _patch_activity(client, order["id"], "cutting", "in_progress")
    assert started.status_code == 200
    data = started.json()["data"]
    assert data["orderStatus"] == "in_process"
    assert data["activity"]["startedAt"] is not None
    assert data["activity"]["startedByLabel"]  # frozen actor

    detail = _order_row(client, order["id"])
    assert detail["status"] == "in_process"
    assert detail["assignedToLabel"]  # taking the order still assigns it


def test_closing_the_last_activity_finishes_the_order(client, db_session):
    """A cut-only order is finished by a single act: closing the cut."""
    order = _order_without_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])

    done = _patch_activity(client, order["id"], "cutting", "done")
    assert done.status_code == 200
    assert done.json()["data"]["orderStatus"] == "finished"

    detail = _order_row(client, order["id"])
    assert detail["status"] == "finished"
    # The derived transition is audited like any other.
    assert detail["history"][-1]["fromStatus"] == "in_process"
    assert detail["history"][-1]["toStatus"] == "finished"


def test_the_order_waits_for_every_activity(client, db_session):
    """Closing the cut with the banding open leaves the order in process."""
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])

    done = _patch_activity(client, order["id"], "cutting", "done")
    assert done.status_code == 200
    assert done.json()["data"]["orderStatus"] == "in_process"

    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )
    closed = _patch_activity(client, order["id"], "banding", "done")
    assert closed.status_code == 200
    assert closed.json()["data"]["orderStatus"] == "finished"


def test_finishing_by_hand_is_blocked_and_names_what_is_missing(client, db_session):
    """The manual path survives for the admin, with the generalized gate."""
    order = _order_with_services(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])

    blocked = _patch_status(client, order["id"], "finished")
    assert blocked.status_code == 422
    message = blocked.json()["errors"][0]["message"].lower()
    assert "canteado" in message and "adicionales" in message


def test_activities_run_in_parallel_with_the_cut(client, db_session):
    """The bander starts while the cut is still open, and it stays open."""
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])

    started = _patch_activity(client, order["id"], "banding", "in_progress")
    assert started.status_code == 200
    assert started.json()["data"]["activity"]["status"] == "in_progress"

    detail = _order_row(client, order["id"])
    assert detail["status"] == "in_process"
    assert _activity(detail, "cutting")["status"] == "in_progress"


# --------------------------------------------------------------------------- #
# Transition guards
# --------------------------------------------------------------------------- #
def test_banding_requires_the_order_in_process(client, db_session):
    """In the queue nothing is cut, so there is nothing to band → 422."""
    order = _order_with_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200
    resp = _patch_activity(client, order["id"], "banding", "in_progress")
    assert resp.status_code == 422
    assert "en proceso" in resp.json()["errors"][0]["message"].lower()


def test_invalid_transition_skipping_in_progress(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    resp = _patch_activity(client, order["id"], "banding", "done")
    assert resp.status_code == 422
    assert "inválida" in resp.json()["errors"][0]["message"].lower()


def test_starting_an_activity_twice_is_idempotent(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    first = _patch_activity(client, order["id"], "banding", "in_progress").json()[
        "data"
    ]
    again = _patch_activity(client, order["id"], "banding", "in_progress").json()[
        "data"
    ]
    # Re-applying doesn't re-stamp the start time.
    assert again["activity"]["startedAt"] == first["activity"]["startedAt"]
    assert again["activity"]["status"] == "in_progress"


# --------------------------------------------------------------------------- #
# Piece floors
# --------------------------------------------------------------------------- #
def test_banding_start_blocked_before_any_banded_piece_is_cut(client, db_session):
    """Taking the order is not the same as having something to band."""
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])

    blocked = _patch_activity(client, order["id"], "banding", "in_progress")
    assert blocked.status_code == 422
    assert "canto" in blocked.json()["errors"][0]["message"].lower()

    # A PLAIN piece releases nothing to band: the floor stays shut.
    plain = _plain_pieces(client, order["id"])
    assert _cut_piece(client, order["id"], plain[0]).status_code == 200
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 422
    )


def test_banding_start_allowed_after_the_first_banded_piece_is_cut(client, db_session):
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    started = _patch_activity(client, order["id"], "banding", "in_progress")
    assert started.status_code == 200


def test_banding_finish_blocked_while_banded_pieces_remain(client, db_session):
    """Finishing needs the LAST banded piece cut, and says how many are missing."""
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )

    blocked = _patch_activity(client, order["id"], "banding", "done")
    assert blocked.status_code == 422
    assert blocked.json()["errors"][0]["message"] == (
        "Faltan 1 pieza(s) con canto por cortar"
    )


def test_banding_finish_allowed_with_plain_pieces_still_uncut(client, db_session):
    """The parallel track survives: only BANDED pieces gate the banding.

    Every banded piece is cut but both plain ones are still pending -- the bander
    finishes anyway, which is the whole point of running the tracks at once.
    """
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    assert _cut_banded_pieces(client, order["id"]) == 2
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )

    finished = _patch_activity(client, order["id"], "banding", "done")
    assert finished.status_code == 200
    assert finished.json()["data"]["activity"]["finishedAt"] is not None
    # The order is NOT finished: the cut is genuinely unfinished.
    assert finished.json()["data"]["orderStatus"] == "in_process"

    still_cutting = _patch_activity(client, order["id"], "cutting", "done")
    assert still_cutting.status_code == 422
    assert "2 pieza(s) por cortar" in still_cutting.json()["errors"][0]["message"]


def test_additional_starts_from_the_first_piece_of_any_kind(client, db_session):
    """Its set is EVERY piece, so a plain one is enough to unblock it."""
    order = _order_with_services(client, db_session)
    _to_in_process(client, order["id"])

    blocked = _patch_activity(client, order["id"], "additional", "in_progress")
    assert blocked.status_code == 422
    assert "ninguna pieza" in blocked.json()["errors"][0]["message"].lower()

    assert (
        _cut_piece(
            client, order["id"], _plain_pieces(client, order["id"])[0]
        ).status_code
        == 200
    )
    assert (
        _patch_activity(client, order["id"], "additional", "in_progress").status_code
        == 200
    )


def test_additional_closes_free_with_pieces_still_uncut(client, db_session):
    """Deliberately no finish floor: there is no per-service piece data to check.

    The bander closes their own work on their word, and the order still waits for
    the cut -- which is what makes the freedom safe.
    """
    order = _order_with_services(client, db_session)
    _to_in_process(client, order["id"])
    assert (
        _cut_piece(
            client, order["id"], _plain_pieces(client, order["id"])[0]
        ).status_code
        == 200
    )
    assert (
        _patch_activity(client, order["id"], "additional", "in_progress").status_code
        == 200
    )

    closed = _patch_activity(client, order["id"], "additional", "done")
    assert closed.status_code == 200
    assert closed.json()["data"]["orderStatus"] == "in_process"


def test_marking_pieces_is_blocked_once_the_cut_is_closed(client, db_session):
    """Unmarking there would break the invariant the closing gate just checked."""
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    piece = _banded_pieces(client, order["id"])[0]
    assert _patch_activity(client, order["id"], "cutting", "done").status_code == 200

    blocked = _cut_piece(client, order["id"], piece, cut=False)
    assert blocked.status_code == 422
    assert "corte en proceso" in blocked.json()["errors"][0]["message"].lower()


def test_marking_pieces_is_blocked_before_the_cut_starts(client, db_session):
    order = _order_without_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    piece = plan["boards"][0]["pieces"][0]
    assert _cut_piece(client, order["id"], piece).status_code == 422


# --------------------------------------------------------------------------- #
# The admin rollback
# --------------------------------------------------------------------------- #
def test_rollback_reopens_the_cut_without_re_dating_the_queue(client, db_session):
    """It undoes somebody taking the wrong order, so the cut never started.

    ``queued_at`` must survive: the client should not go to the back of the line
    for a mistake that was not theirs.
    """
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    queued_at = _order_row(client, order["id"])["queuedAt"]

    assert _patch_status(client, order["id"], "queued").status_code == 200
    detail = _order_row(client, order["id"])
    assert detail["status"] == "queued"
    assert detail["queuedAt"] == queued_at
    assert detail["assignedToLabel"] is None
    cut = _activity(detail, "cutting")
    assert cut["status"] == "pending"
    assert cut["startedAt"] is None


# --------------------------------------------------------------------------- #
# Shop-floor RBAC
# --------------------------------------------------------------------------- #
def test_canteador_can_band_but_not_read_order_detail(client, db_session):
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    headers = _token_for(client, db_session, "canteador")

    # Can register their activity and see their workshop board...
    assert (
        _patch_activity(
            client, order["id"], "banding", "in_progress", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/orders/workshop-queue", headers=headers).status_code == 200
    )
    # ...but NOT the order detail (no orders:read).
    assert (
        client.get(f"/api/v1/orders/{order['id']}", headers=headers).status_code == 403
    )


def test_each_role_only_registers_its_own_activities(client, db_session):
    """The operator cuts; the bander bands and does the additional work."""
    order = _order_with_services(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    operator = _token_for(client, db_session, "operador")
    bander = _token_for(client, db_session, "canteador")

    for activity in ("banding", "additional"):
        resp = _patch_activity(
            client, order["id"], activity, "in_progress", headers=operator
        )
        assert resp.status_code == 403, activity
    assert (
        _patch_activity(
            client, order["id"], "cutting", "in_progress", headers=bander
        ).status_code
        == 403
    )
    # The seller is not shop floor at all.
    seller = _token_for(client, db_session, "vendedor")
    assert (
        _patch_activity(
            client, order["id"], "banding", "in_progress", headers=seller
        ).status_code
        == 403
    )


def test_the_bander_finishes_an_order_the_operator_left(client, db_session):
    """The operator moves on; closing the banding is what finishes the order."""
    order = _order_with_banding(client, db_session)
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_activity(client, order["id"], "cutting", "done").status_code == 200

    bander = _token_for(client, db_session, "canteador")
    assert (
        _patch_activity(
            client, order["id"], "banding", "in_progress", headers=bander
        ).status_code
        == 200
    )
    closed = _patch_activity(client, order["id"], "banding", "done", headers=bander)
    assert closed.status_code == 200
    assert closed.json()["data"]["orderStatus"] == "finished"


# --------------------------------------------------------------------------- #
# The per-activity clocks (``ready_at``)
# --------------------------------------------------------------------------- #
def _plain_pieces(client, oid):
    """Placed pieces with NO edge banding — the ones that must not start the clock."""
    plan = client.get(f"/api/v1/orders/{oid}/cutting-plan").json()["data"]
    return [p for board in plan["boards"] for p in board["pieces"] if not p["edges"]]


def _ready_at(client, oid, activity):
    return _activity(_order_row(client, oid), activity)["readyAt"]


def test_the_cut_is_ready_when_the_order_is_paid(client, db_session):
    """Reaching the queue is the moment the cut stopped being blocked."""
    order = _order_without_banding(client, db_session)
    assert _ready_at(client, order["id"], "cutting") is None
    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert _ready_at(client, order["id"], "cutting") is not None


def test_banding_clock_stays_null_while_the_bander_is_blocked(client, db_session):
    """A pending activity nobody could have worked on must show no clock.

    The whole point of dating this from the floor rather than from the order's
    creation: while no banded piece is cut the bander cannot start, so counting
    would put somebody in red for work they were not allowed to do.
    """
    order = _order_mixed_pieces(client, db_session)
    assert _ready_at(client, order["id"], "banding") is None
    _to_in_process(client, order["id"])
    assert _ready_at(client, order["id"], "banding") is None


def test_plain_piece_does_not_start_the_banding_clock(client, db_session):
    """Cutting a piece with no canto is not the banding's floor opening.

    Guards the JSON-null trap from the other side: `edges` reads back as the JSON
    value ``null``, so a check written as "is not null" would count this piece.
    """
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    assert (
        _cut_piece(
            client, order["id"], _plain_pieces(client, order["id"])[0]
        ).status_code
        == 200
    )
    assert _ready_at(client, order["id"], "banding") is None


def test_first_banded_piece_starts_the_banding_clock(client, db_session):
    order = _order_mixed_pieces(client, db_session)
    _to_in_process(client, order["id"])
    _cut_first_banded_piece(client, order["id"])
    assert _ready_at(client, order["id"], "banding") is not None


def test_banding_clock_is_sealed_once_and_survives_unmarking(client, db_session):
    """Frozen on the first banded piece, like ``queued_at`` on the first enqueue.

    Cutting a second piece must not re-date it, and unmarking the first must not
    erase it: the floor did open, and somebody else's misclick should not give
    the bander's waiting time back.
    """
    order = _order_mixed_pieces(client, db_session)
    oid = order["id"]
    _to_in_process(client, oid)
    banded = _banded_pieces(client, oid)
    assert _cut_piece(client, oid, banded[0]).status_code == 200
    sealed = _ready_at(client, oid, "banding")
    assert sealed is not None

    assert _cut_piece(client, oid, banded[1]).status_code == 200
    assert _ready_at(client, oid, "banding") == sealed

    assert _cut_piece(client, oid, banded[0], cut=False).status_code == 200
    assert _ready_at(client, oid, "banding") == sealed
