"""Integration: cancelling an order, and what that is allowed to cost.

Two origins, two different rules. From ``confirmed`` the quote simply died and
admin/seller both retire it. From ``queued`` the client has already PAID (the
payment is what gates the way into the queue), so it is the exceptional exit:
ADMIN ONLY and the money already collected stays on the row. (The fan-out to
the branch's operators lives in ``test_notifications.py``, next to the rest of
the notification audiences.)

A reason is required on both, because the history row's ``note`` is the only
record a cancellation leaves -- there is no reason column and no cancelled_by.
"""

from src.modules.orders.model import OrderStatusHistoryModel
from tests.order_helpers import (
    _order_without_banding,
    _patch_status,
    _to_finished,
    _token_for,
)


def _history(db_session, order_id):
    return (
        db_session.query(OrderStatusHistoryModel)
        .filter(OrderStatusHistoryModel.order_id == order_id)
        .order_by(OrderStatusHistoryModel.id)
        .all()
    )


def _detail(client, order_id):
    return client.get(f"/api/v1/orders/{order_id}").json()["data"]


# --------------------------------------------------------------------------- #
# The new exit: queued -> cancelled
# --------------------------------------------------------------------------- #
def test_admin_cancels_a_queued_order(client, db_session):
    order = _order_without_banding(client, db_session)
    assert _patch_status(client, order["id"], "queued").status_code == 200

    resp = _patch_status(
        client, order["id"], "cancelled", note="El cliente desistió del pedido"
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"

    # The reason is the audit trail: it rides on the transition's history row.
    last = _history(db_session, order["id"])[-1]
    assert (last.from_status, last.to_status) == ("queued", "cancelled")
    assert last.note == "El cliente desistió del pedido"

    # What was collected survives: erasing it would forge the history.
    detail = _detail(client, order["id"])
    assert detail["paymentCashAmount"] == 100.0
    assert detail["queuedAt"] is not None


def test_a_cancelled_order_leaves_the_workshop_board(client, db_session):
    order = _order_without_banding(client, db_session)
    _patch_status(client, order["id"], "queued")
    board = client.get("/api/v1/orders/workshop-queue").json()["data"]
    assert order["id"] in [item["orderId"] for item in board]

    _patch_status(client, order["id"], "cancelled", note="Cliente desistió")
    board = client.get("/api/v1/orders/workshop-queue").json()["data"]
    assert order["id"] not in [item["orderId"] for item in board]


def test_only_the_admin_cancels_from_the_queue(client, db_session):
    """Not even the seller who raised it: the sale was already collected."""
    cedulas = ("0100000900", "0100000918", "0100000926")
    for idx, role in enumerate(("vendedor", "operador", "canteador")):
        order = _order_without_banding(client, db_session, identifier=cedulas[idx])
        _patch_status(client, order["id"], "queued")
        headers = _token_for(client, db_session, role)

        resp = _patch_status(
            client, order["id"], "cancelled", note="motivo", headers=headers
        )
        assert resp.status_code == 403, role
        # The rejected transition does not move the order.
        assert _detail(client, order["id"])["status"] == "queued", role


# --------------------------------------------------------------------------- #
# The reason, on both origins
# --------------------------------------------------------------------------- #
def test_cancelling_a_queued_order_requires_a_reason(client, db_session):
    order = _order_without_banding(client, db_session)
    _patch_status(client, order["id"], "queued")

    resp = _patch_status(client, order["id"], "cancelled", note="   ")
    assert resp.status_code == 422
    assert "motivo" in resp.json()["errors"][0]["message"]
    assert _detail(client, order["id"])["status"] == "queued"


def test_cancelling_a_confirmed_order_requires_a_reason_too(client, db_session):
    """The older exit changed: it used to accept an empty note."""
    order = _order_without_banding(client, db_session)

    assert _patch_status(client, order["id"], "cancelled", note="").status_code == 422
    assert _detail(client, order["id"])["status"] == "confirmed"

    seller = _token_for(client, db_session, "vendedor")
    resp = _patch_status(
        client, order["id"], "cancelled", note="Cotización descartada", headers=seller
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"


def test_an_impossible_cancellation_still_reports_itself_as_invalid(client, db_session):
    """Gate order: the graph is checked before the reason.

    Answering "indica el motivo" here would say the move works if only you typed
    something, when in fact a dispatched order has no way out at all.
    """
    order = _order_without_banding(client, db_session)
    _to_finished(client, order["id"])
    assert _patch_status(client, order["id"], "dispatched").status_code == 200

    resp = _patch_status(client, order["id"], "cancelled", note="")
    assert resp.status_code == 422
    assert "inválida" in resp.json()["errors"][0]["message"]
