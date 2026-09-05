"""Tests for priority attention on orders: ``PATCH /orders/{id}/priority``.

The workshop board hands orders out FIFO; sales marks the exceptional order that
must be attended first. The flag reorders ``GET /orders/workshop-queue`` (and
only that board), filters the admin listing, and is refused to the shop floor --
seeing the highlight is not the same as being able to jump your own queue.
Reuses the catalog/order/token helpers of the banding-track suite.
"""

from tests.test_order_banding import (
    _cut_all_pieces,
    _order_with_banding,
    _patch_banding,
    _patch_status,
    _to_cutting,
    _token_for,
)

_URL = "/api/v1/orders"
_QUEUE = "/api/v1/orders/workshop-queue"


def _set_priority(client, oid, is_priority, **kw):
    return client.patch(
        f"{_URL}/{oid}/priority", json={"isPriority": is_priority}, **kw
    )


def _board_ids(client, ids, **kw):
    """Board order, restricted to the orders a test cares about."""
    board = client.get(_QUEUE, **kw).json()["data"]
    return [i["orderId"] for i in board if i["orderId"] in ids]


def test_priority_order_jumps_the_queue(client, db_session):
    """A later order marked priority is listed before the ones that arrived first."""
    first = _order_with_banding(client, db_session, identifier="0100000181")
    second = _order_with_banding(client, db_session, identifier="0100000199")
    third = _order_with_banding(client, db_session, identifier="0100000207")
    for order in (first, second, third):
        _to_cutting(client, order["id"])
    ids = {o["id"] for o in (first, second, third)}
    assert _board_ids(client, ids) == [first["id"], second["id"], third["id"]]

    assert _set_priority(client, third["id"], True).status_code == 200
    assert _board_ids(client, ids) == [third["id"], first["id"], second["id"]]

    item = next(
        i for i in client.get(_QUEUE).json()["data"] if i["orderId"] == third["id"]
    )
    assert item["isPriority"] is True


def test_priority_keeps_fifo_among_prioritized_orders(client, db_session):
    """Priority is a group, not a ranking: FIFO still breaks the tie inside it."""
    first = _order_with_banding(client, db_session, identifier="0100000215")
    second = _order_with_banding(client, db_session, identifier="0100000223")
    third = _order_with_banding(client, db_session, identifier="0100000231")
    for order in (first, second, third):
        _to_cutting(client, order["id"])
    ids = {o["id"] for o in (first, second, third)}

    # Marked youngest-first, so the board can't be echoing the order they were marked in.
    assert _set_priority(client, third["id"], True).status_code == 200
    assert _set_priority(client, second["id"], True).status_code == 200
    assert _board_ids(client, ids) == [second["id"], third["id"], first["id"]]


def test_priority_can_be_withdrawn(client, db_session):
    """Reversible: unmarking puts the order back in its FIFO place."""
    first = _order_with_banding(client, db_session, identifier="0100000249")
    second = _order_with_banding(client, db_session, identifier="0100000256")
    _to_cutting(client, first["id"])
    _to_cutting(client, second["id"])
    ids = {first["id"], second["id"]}

    assert _set_priority(client, second["id"], True).status_code == 200
    assert _board_ids(client, ids) == [second["id"], first["id"]]

    resp = _set_priority(client, second["id"], False)
    assert resp.status_code == 200
    assert resp.json()["data"]["isPriority"] is False
    assert _board_ids(client, ids) == [first["id"], second["id"]]


def test_priority_records_history_and_is_idempotent(client, db_session):
    """One history row per real change; re-marking writes nothing."""
    order = _order_with_banding(client, db_session, identifier="0100000264")
    _to_cutting(client, order["id"])
    before = len(client.get(f"{_URL}/{order['id']}").json()["data"]["history"])

    assert _set_priority(client, order["id"], True).status_code == 200
    history = client.get(f"{_URL}/{order['id']}").json()["data"]["history"]
    assert len(history) == before + 1
    entry = history[-1]
    # Not a state transition: from == to, and the note says what happened.
    assert entry["fromStatus"] == entry["toStatus"] == "cutting"
    assert entry["note"] == "Marcada como prioritaria"

    # Re-marking is a no-op: no second row.
    assert _set_priority(client, order["id"], True).status_code == 200
    assert (
        len(client.get(f"{_URL}/{order['id']}").json()["data"]["history"]) == before + 1
    )


def test_priority_accepts_a_note(client, db_session):
    """The reason travels into the order's timeline instead of the default text."""
    order = _order_with_banding(client, db_session, identifier="0100000272")
    _to_cutting(client, order["id"])
    resp = client.patch(
        f"{_URL}/{order['id']}/priority",
        json={"isPriority": True, "note": "Cliente viaja mañana"},
    )
    assert resp.status_code == 200
    history = client.get(f"{_URL}/{order['id']}").json()["data"]["history"]
    assert history[-1]["note"] == "Cliente viaja mañana"


def test_priority_is_refused_on_a_closed_order(client, db_session):
    """Prioritizing a completed order means nothing -- the board doesn't list it."""
    order = _order_with_banding(client, db_session, identifier="0100000280")
    _to_cutting(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_status(client, order["id"], "cut").status_code == 200
    assert _patch_banding(client, order["id"], "in_progress").status_code == 200
    assert _patch_banding(client, order["id"], "done").status_code == 200
    assert _patch_status(client, order["id"], "completed").status_code == 200

    resp = _set_priority(client, order["id"], True)
    assert resp.status_code == 422
    assert "cerrada" in resp.json()["errors"][0]["message"]


def test_priority_rbac(client, db_session):
    """Admin/seller mark it; the shop floor sees the flag but cannot set it."""
    order = _order_with_banding(client, db_session, identifier="0100000298")
    _to_cutting(client, order["id"])

    # The two workshop roles lack ``orders:write``: they must not jump their own queue.
    for role in ("operador", "canteador"):
        headers = _token_for(client, db_session, role)
        resp = _set_priority(client, order["id"], True, headers=headers)
        assert resp.status_code == 403

    seller = _token_for(client, db_session, "vendedor")
    assert _set_priority(client, order["id"], True, headers=seller).status_code == 200

    # ...and the operator does see the resulting flag on their board.
    operator = _token_for(client, db_session, "operador")
    board = client.get(_QUEUE, headers=operator).json()["data"]
    item = next(i for i in board if i["orderId"] == order["id"])
    assert item["isPriority"] is True


def test_priority_on_an_unknown_order_is_404(client, db_session):
    """Scoped lookup: an order the caller can't reach is a uniform 404."""
    assert _set_priority(client, 999999, True).status_code == 404


def test_board_fifo_follows_the_queue_entry_not_the_creation(client, db_session):
    """Arrival is when the order was PAID, not when it was quoted.

    The gate into the queue is the payment, so a quote raised first can be paid
    last. Ordering the board by creation handed the first slot to whoever asked
    first instead of whoever paid first.
    """
    first_quoted = _order_with_banding(client, db_session, identifier="0100000330")
    second_quoted = _order_with_banding(client, db_session, identifier="0100000348")
    ids = {first_quoted["id"], second_quoted["id"]}

    # The one quoted SECOND pays first, so it reaches the shop first.
    assert _patch_status(client, second_quoted["id"], "queued").status_code == 200
    assert _patch_status(client, first_quoted["id"], "queued").status_code == 200

    assert _board_ids(client, ids) == [second_quoted["id"], first_quoted["id"]]

    board = client.get(_QUEUE).json()["data"]
    item = next(i for i in board if i["orderId"] == second_quoted["id"])
    assert item["queuedAt"] is not None


def test_priority_still_wins_over_the_queue_entry(client, db_session):
    """The two rules compose: priority first, and inside it the real arrival order."""
    early = _order_with_banding(client, db_session, identifier="0100000355")
    late = _order_with_banding(client, db_session, identifier="0100000363")
    assert _patch_status(client, early["id"], "queued").status_code == 200
    assert _patch_status(client, late["id"], "queued").status_code == 200
    ids = {early["id"], late["id"]}
    assert _board_ids(client, ids) == [early["id"], late["id"]]

    assert _set_priority(client, late["id"], True).status_code == 200
    assert _board_ids(client, ids) == [late["id"], early["id"]]


def test_admin_rollback_keeps_the_order_place_in_line(client, db_session):
    """``cutting → queued`` undoes a wrong take; it must not re-date the arrival.

    Sending the order to the back of the line would charge the client for a
    mistake that was not theirs.
    """
    first = _order_with_banding(client, db_session, identifier="0100000371")
    second = _order_with_banding(client, db_session, identifier="0100000389")
    assert _patch_status(client, first["id"], "queued").status_code == 200
    assert _patch_status(client, second["id"], "queued").status_code == 200
    ids = {first["id"], second["id"]}

    queued_at = client.get(f"{_URL}/{first['id']}").json()["data"]["queuedAt"]

    # Taken by mistake and rolled back by the admin.
    assert _patch_status(client, first["id"], "cutting").status_code == 200
    assert _patch_status(client, first["id"], "queued").status_code == 200

    assert client.get(f"{_URL}/{first['id']}").json()["data"]["queuedAt"] == queued_at
    assert _board_ids(client, ids) == [first["id"], second["id"]]


def test_queued_at_is_null_until_the_order_is_paid(client, db_session):
    """A ``confirmed`` order has not reached the shop, so it has no arrival time."""
    order = _order_with_banding(client, db_session, identifier="0100000397")
    assert client.get(f"{_URL}/{order['id']}").json()["data"]["queuedAt"] is None
    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert client.get(f"{_URL}/{order['id']}").json()["data"]["queuedAt"] is not None


def test_orders_listing_filters_by_priority(client, db_session):
    """``?isPriority`` narrows the listing; it never reorders it."""
    plain = _order_with_banding(client, db_session, identifier="0100000314")
    urgent = _order_with_banding(client, db_session, identifier="0100000322")
    assert _set_priority(client, urgent["id"], True).status_code == 200

    only_priority = client.get(f"{_URL}/?isPriority=true").json()["data"]
    assert [o["id"] for o in only_priority] == [urgent["id"]]

    only_plain = [
        o["id"] for o in client.get(f"{_URL}/?isPriority=false").json()["data"]
    ]
    assert plain["id"] in only_plain
    assert urgent["id"] not in only_plain

    # Omitted: both, in the endpoint's own order (oldest first), unmoved by the flag.
    both = [o["id"] for o in client.get(f"{_URL}/").json()["data"]]
    assert both.index(plain["id"]) < both.index(urgent["id"])
