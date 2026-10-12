"""Money received after the sale: the credit an order was sold on, paid back.

The sale freezes how it was paid for (cash, transfer and CREDIT) when the order
enters the queue. The credit used to read as owed for ever; every payment since
is a row of ``order_payments`` and the balance is the credit less those rows.
The usual moment is the dispatch, which takes the payment in the same request --
and never blocks on a balance, though it then asks whose decision it was.
"""

import io

from pypdf import PdfReader

from tests.order_helpers import (
    _cut_all_pieces,
    _order_without_banding,
    _patch_activity,
    _patch_status,
    _token_for,
)


def _queue(client, oid, cash=20.0, credit=80.0):
    """Pays the order into the queue, part of it on credit."""
    payment = {"cashAmount": cash}
    if credit:
        payment["creditAmount"] = credit
    resp = client.patch(
        f"/api/v1/orders/{oid}/status",
        json={
            "status": "queued",
            "payment": payment,
            "externalInvoiceId": f"FAC-{oid:04d}",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _finish(client, oid):
    """The shop floor's ladder on an order with nothing but the cut."""
    assert _patch_activity(client, oid, "cutting", "in_progress").status_code == 200
    _cut_all_pieces(client, oid)
    assert _patch_activity(client, oid, "cutting", "done").status_code == 200


def _pay(client, oid, **kw):
    headers = kw.pop("headers", None)
    return client.post(f"/api/v1/orders/{oid}/payments", json=kw, headers=headers)


def _dispatch(client, oid, note=None, payment=None, **kw):
    body = {"status": "dispatched"}
    if note is not None:
        body["note"] = note
    if payment is not None:
        body["payment"] = payment
    return client.patch(f"/api/v1/orders/{oid}/status", json=body, **kw)


def _error(resp):
    return resp.json()["errors"][0]


def _order(client, oid):
    return client.get(f"/api/v1/orders/{oid}").json()["data"]


# ------------------------------------------------------------------ the balance
def test_the_credit_sold_is_what_the_order_owes(client, db_session):
    order = _order_without_banding(client, db_session)
    assert order["creditBalance"] == 0  # not sold yet

    queued = _queue(client, order["id"])

    assert queued["paymentCreditAmount"] == 80.0
    assert (queued["creditCollected"], queued["creditBalance"]) == (0, 80.0)
    assert queued["payments"] == []


def test_a_payment_brings_the_balance_down(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])

    resp = _pay(client, order["id"], cashAmount=30.0, transferAmount=20.0, note="Abono")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert (data["creditCollected"], data["creditBalance"]) == (50.0, 30.0)
    # One row per method, and the sale's own terms untouched.
    assert [(p["method"], p["amount"]) for p in data["payments"]] == [
        ("cash", 30.0),
        ("transfer", 20.0),
    ]
    assert data["payments"][0]["receivedByLabel"] == "Conftest Admin"
    assert data["payments"][0]["note"] == "Abono"
    assert data["paymentCreditAmount"] == 80.0
    row = data["history"][-1]
    assert row["fromStatus"] == row["toStatus"] == "queued"
    assert row["note"] == "Abono: efectivo $30.00 · transferencia $20.00 · saldo $30.00"


def test_a_payment_cannot_exceed_what_is_owed(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])

    over = _pay(client, order["id"], cashAmount=80.01 + 0.01)
    assert over.status_code == 422
    assert _error(over)["field"] == "payment"
    assert "excede el saldo" in _error(over)["message"]

    assert _pay(client, order["id"], cashAmount=80.0).status_code == 200
    # Settled: there is nothing left to pay.
    assert _pay(client, order["id"], cashAmount=1.0).status_code == 422
    assert _order(client, order["id"])["creditBalance"] == 0


def test_a_payment_needs_an_amount(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    empty = _pay(client, order["id"], cashAmount=0)
    assert empty.status_code == 422
    assert _error(empty)["field"] == "payment"


def test_an_order_not_yet_sold_or_cancelled_takes_no_payment(client, db_session):
    order = _order_without_banding(client, db_session)
    confirmed = _pay(client, order["id"], cashAmount=10.0)
    assert confirmed.status_code == 422

    _queue(client, order["id"])
    assert _patch_status(client, order["id"], "cancelled").status_code == 200
    cancelled = _pay(client, order["id"], cashAmount=10.0)
    assert cancelled.status_code == 422
    assert "cancelada" in _error(cancelled)["message"]
    # A cancelled order owes nothing, whatever it was sold for.
    assert _order(client, order["id"])["creditBalance"] == 0


def test_the_seller_collects_and_the_shop_floor_does_not(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    seller = _token_for(client, db_session, "vendedor")
    operator = _token_for(client, db_session, "operador")

    assert (
        _pay(client, order["id"], cashAmount=10.0, headers=operator).status_code == 403
    )
    assert _pay(client, order["id"], cashAmount=10.0, headers=seller).status_code == 200


# ----------------------------------------------------------------- the dispatch
def test_dispatching_collects_what_is_owed_in_the_same_request(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    _finish(client, order["id"])

    resp = _dispatch(
        client, order["id"], payment={"cashAmount": 50.0, "transferAmount": 30.0}
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "dispatched"
    assert (data["creditCollected"], data["creditBalance"]) == (80.0, 0)
    assert [p["method"] for p in data["payments"]] == ["cash", "transfer"]
    # The payment is stamped with the dispatch's own instant.
    assert data["payments"][0]["paidAt"] == data["dispatchedAt"]
    transition, payment_row = data["history"][-2:]
    assert (transition["fromStatus"], transition["toStatus"]) == (
        "finished",
        "dispatched",
    )
    assert payment_row["note"].startswith("Abono al despachar: efectivo $50.00")


def test_dispatching_with_a_balance_is_allowed_but_somebody_answers_for_it(
    client, db_session
):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    _finish(client, order["id"])

    silent = _dispatch(client, order["id"], payment={"cashAmount": 50.0})
    assert silent.status_code == 422
    assert _error(silent)["field"] == "note"
    assert "$30.00" in _error(silent)["message"]
    # Nothing moved and nothing was collected by the rejected attempt.
    untouched = _order(client, order["id"])
    assert (untouched["status"], untouched["payments"]) == ("finished", [])

    ok = _dispatch(
        client,
        order["id"],
        note="Autoriza gerencia, paga el viernes",
        payment={"cashAmount": 50.0},
    )
    assert ok.status_code == 200, ok.text
    assert (ok.json()["data"]["status"], ok.json()["data"]["creditBalance"]) == (
        "dispatched",
        30.0,
    )


def test_an_order_with_nothing_owed_dispatches_as_it_always_did(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"], cash=100.0, credit=0)
    _finish(client, order["id"])
    assert _dispatch(client, order["id"]).status_code == 200


def test_the_dispatch_rejects_a_payment_that_is_not_money(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    _finish(client, order["id"])

    on_credit = _dispatch(client, order["id"], note="x", payment={"creditAmount": 80.0})
    assert on_credit.status_code == 422
    assert _error(on_credit)["field"] == "payment"
    too_much = _dispatch(client, order["id"], payment={"cashAmount": 500.0})
    assert too_much.status_code == 422
    assert _order(client, order["id"])["status"] == "finished"


def test_the_rest_can_be_paid_after_the_dispatch(client, db_session):
    """A dispatched order no longer changes status -- and still takes payments."""
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    _finish(client, order["id"])
    _dispatch(
        client, order["id"], note="Paga a fin de mes", payment={"cashAmount": 50.0}
    )

    resp = _pay(client, order["id"], transferAmount=30.0, note="Transf. 4471")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert (data["status"], data["creditBalance"]) == ("dispatched", 0)
    assert data["history"][-1]["fromStatus"] == "dispatched"


# --------------------------------------------------------------------- the void
def test_voiding_a_payment_puts_the_balance_back(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    paid = _pay(client, order["id"], cashAmount=80.0).json()["data"]
    payment_id = paid["payments"][0]["id"]
    url = f"/api/v1/orders/{order['id']}/payments/{payment_id}/void"

    seller = _token_for(client, db_session, "vendedor")
    assert client.post(url, json={"reason": "x"}, headers=seller).status_code == 403
    assert client.post(url, json={"reason": "  "}).status_code == 422

    resp = client.post(url, json={"reason": "Eran $8, no $80"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert (data["creditCollected"], data["creditBalance"]) == (0, 80.0)
    [payment] = data["payments"]  # the row stays, flagged
    assert payment["voidedAt"] is not None
    assert payment["voidReason"] == "Eran $8, no $80"
    assert payment["voidedByLabel"] == "Conftest Admin"
    assert data["history"][-1]["note"] == (
        "Abono anulado: efectivo $80.00 — Eran $8, no $80"
    )
    # Idempotent: a second void writes no second row.
    again = client.post(url, json={"reason": "otra vez"}).json()["data"]
    assert len(again["history"]) == len(data["history"])
    assert again["payments"][0]["voidReason"] == "Eran $8, no $80"


def test_voiding_an_unknown_payment_is_a_404(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])
    resp = client.post(
        f"/api/v1/orders/{order['id']}/payments/999/void", json={"reason": "x"}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------- where it shows
def test_the_listing_filters_by_what_is_still_owed(client, db_session):
    owing = _order_without_banding(client, db_session)
    _queue(client, owing["id"])
    settled = _order_without_banding(client, db_session, identifier="0100000900")
    _queue(client, settled["id"])
    _pay(client, settled["id"], cashAmount=80.0)
    cash_only = _order_without_banding(client, db_session, identifier="0100000918")
    _queue(client, cash_only["id"], cash=100.0, credit=0)

    def listed(has_balance):
        resp = client.get("/api/v1/orders/", params={"hasBalance": has_balance})
        return {item["id"] for item in resp.json()["data"]}

    assert listed("true") == {owing["id"]}
    assert listed("false") == {settled["id"], cash_only["id"]}


def _document_text(client, oid):
    pdf = client.get(f"/api/v1/orders/{oid}/document").content
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def test_the_document_prints_what_was_paid_and_what_is_left(client, db_session):
    order = _order_without_banding(client, db_session)
    _queue(client, order["id"])

    before = _document_text(client, order["id"])
    assert "A crédito:" in before
    assert "Abonos recibidos" not in before and "Saldo pendiente" not in before

    _pay(client, order["id"], cashAmount=50.0)
    after = _document_text(client, order["id"])
    assert "Abonos recibidos:" in after and "$50.00" in after
    assert "Saldo pendiente:" in after and "$30.00" in after
