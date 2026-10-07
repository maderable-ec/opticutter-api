"""Branch comparison, production tab and productivity by role (``performance.py``).

Seeded straight through ``db_session``, like ``test_analytics.py``: the figures
hinge on exact instants (a payment, the last piece of a sheet, a closing), which
the state machine would stamp with the real clock.

Every instant is written as Ecuador's wall clock (``_at``) and stored as the
naive UTC the app writes: the report days are the business's, UTC-5.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    OrderActivityModel,
    OrderBoardModel,
    OrderLineModel,
    OrderModel,
    OrderPlacedPieceModel,
    OrderStatusHistoryModel,
)
from src.modules.users.model import UserModel

_RANGE = {"from": "2026-06-01", "to": "2026-06-30"}
_MATERIALS = [
    {"material_key": "m", "source": "catalog"},
    {"material_key": "r", "source": "clientOffcut"},
]


def _at(day, hour, minute=0, month=6):
    """Ecuador's wall clock (UTC-5) as the naive UTC the app stores."""
    return datetime(2026, month, day, hour, minute) + timedelta(hours=5)


def _client(db):
    client = ClientModel(identifier="0100000397")
    db.add(client)
    db.commit()
    return client


def _branch(db, code="MACAS", name="Macas", active=True):
    branch = BranchModel(code=code, name=name, is_active=active)
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch


def _user(db, name, roles, branch_id=1):
    user = UserModel(
        email=f"{name.replace(' ', '').lower()}@e.com",
        full_name=name,
        hashed_password="x",
        roles=roles,
        branch_id=branch_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _order(
    db,
    client_id,
    *,
    branch_id=1,
    status="queued",
    total=100.0,
    created_at=None,
    queued_at=None,
    cash=None,
    transfer=None,
    credit=None,
    created_by=None,
    code=None,
    history=None,
    lines=None,
    banding_m=None,
):
    created_at = created_at or _at(2, 10)
    snapshot = {"materials": _MATERIALS}
    if banding_m is not None:
        snapshot["total_edge_banding_linear_m"] = banding_m
    order = OrderModel(
        client_id=client_id,
        branch_id=branch_id,
        status=status,
        optimization_snapshot=snapshot,
        optimization_hash="h",
        currency="USD",
        subtotal=total,
        total=total,
        total_boards_used=1,
        created_at=created_at,
        confirmed_at=created_at,
        queued_at=queued_at,
        payment_cash_amount=cash,
        payment_transfer_amount=transfer,
        payment_credit_amount=credit,
        created_by=created_by,
        code=code,
    )
    if history is not None:
        order.history = history
    if lines is not None:
        order.lines = lines
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _board(db, order_id, *, half=False, material_key="m", cuts=(2440.0, 1220.0)):
    board = OrderBoardModel(
        order_id=order_id,
        sheet_number=1,
        material_key=material_key,
        width=1220 if half else 2440,
        height=2440,
        thickness=18,
        half_board=half,
        cuts=[
            {"x": 0, "y": 0, "length": length, "is_horizontal": True} for length in cuts
        ]
        if cuts is not None
        else None,
    )
    db.add(board)
    db.commit()
    db.refresh(board)
    return board


def _piece(db, order_id, board_id, cut_at=None, cut_by=None, n=1):
    db.add(
        OrderPlacedPieceModel(
            order_id=order_id,
            board_id=board_id,
            piece_id=f"p#{board_id}-{n}",
            label="p",
            x=0,
            y=0,
            width=600,
            height=400,
            original_width=600,
            original_height=400,
            rotated=False,
            cut_at=cut_at,
            cut_by=cut_by,
        )
    )
    db.commit()


def _activity(db, order_id, kind, status="done", **clocks):
    db.add(OrderActivityModel(order_id=order_id, type=kind, status=status, **clocks))
    db.commit()


def _finished(at):
    return OrderStatusHistoryModel(
        from_status="in_process", to_status="finished", actor="staff", created_at=at
    )


def _get(client, path, **params):
    resp = client.get(f"/api/v1/analytics/{path}", params={**_RANGE, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _seed_cut_day(db, client_id, operator, closer=None):
    """One morning on branch 1 (June 10th, local), the worked example.

    Events: 07:55 take the cut, pieces at 08:00, 08:06, 08:10, 08:40, 08:45 and
    08:50. Gaps 5, 6, 4, 30, 5 and 5 minutes: 25 min of work and one 30-min stop.
    Sheets: a whole board (3.66 m) closed at 08:06, a half (1.22 m) closed at
    08:40 -- by ``closer`` if given --, a client retazo (0.50 m) at 08:45, and a
    board left half cut (one piece pending) that is not processed.
    """
    closer = closer or operator
    order = _order(db, client_id, status="in_process", queued_at=_at(9, 9), cash=10.0)
    _activity(
        db,
        order.id,
        "cutting",
        status="in_progress",
        started_at=_at(10, 7, 55),
        started_by=operator.id,
    )
    whole = _board(db, order.id)
    _piece(db, order.id, whole.id, _at(10, 8, 0), operator.id, n=1)
    _piece(db, order.id, whole.id, _at(10, 8, 6), operator.id, n=2)
    half = _board(db, order.id, half=True, cuts=(1220.0,))
    _piece(db, order.id, half.id, _at(10, 8, 10), operator.id, n=1)
    _piece(db, order.id, half.id, _at(10, 8, 40), closer.id, n=2)
    retazo = _board(db, order.id, material_key="r", cuts=(500.0,))
    _piece(db, order.id, retazo.id, _at(10, 8, 45), operator.id)
    pending = _board(db, order.id)
    _piece(db, order.id, pending.id, _at(10, 8, 50), operator.id, n=1)
    _piece(db, order.id, pending.id, None, None, n=2)
    return order


# -------------------------------------------------------------- the comparison
def test_every_active_branch_is_a_column_even_at_zero(client, db_session):
    _branch(db_session)
    _branch(db_session, code="OLD", name="Cerrada", active=False)

    data = _get(client, "branch-comparison")

    assert [b["branchName"] for b in data["branches"]] == ["Casa Matriz", "Macas"]
    assert data["idleMinutes"] == 15
    total = data["total"]
    assert total["branchId"] is None
    assert total["sales"] == {"cash": 0, "credit": 0, "total": 0, "paidOrders": 0}
    assert total["orders"] == {"entered": 0, "finished": 0}
    assert total["production"]["boards"] == 0
    assert total["production"]["boardsPerHour"] == 0
    assert data["range"] == {"dateFrom": "2026-06-01", "dateTo": "2026-06-30"}


def test_a_sale_is_placed_by_its_payment_and_transfer_counts_as_cash(
    client, db_session
):
    macas = _branch(db_session)
    c = _client(db_session)
    # Created in May, PAID in June: a June sale.
    _order(
        db_session,
        c.id,
        created_at=_at(28, 10, month=5),
        queued_at=_at(10, 10),
        cash=50.0,
        transfer=30.0,
        credit=20.0,
    )
    _order(db_session, c.id, branch_id=macas.id, queued_at=_at(12, 10), credit=40.0)
    # Cancelled after paying: the amounts stay as a record, not as a sale.
    _order(db_session, c.id, status="cancelled", queued_at=_at(15, 10), cash=999.0)
    # 23:00 on May 31st, local -- 04:00 UTC on June 1st: still May.
    _order(
        db_session,
        c.id,
        created_at=_at(30, 10, month=5),
        queued_at=_at(31, 23, month=5),
        cash=999.0,
    )
    # Confirmed, not paid yet: an order that came in, not a sale.
    _order(db_session, c.id, status="confirmed", created_at=_at(20, 10))

    data = _get(client, "branch-comparison")
    matriz, other = data["branches"]

    assert matriz["sales"] == {
        "cash": 80.0,
        "credit": 20.0,
        "total": 100.0,
        "paidOrders": 1,
    }
    assert other["sales"] == {"cash": 0, "credit": 40.0, "total": 40.0, "paidOrders": 1}
    assert data["total"]["sales"] == {
        "cash": 80.0,
        "credit": 60.0,
        "total": 140.0,
        "paidOrders": 2,
    }
    # Entered by creation in range: the cancelled one, Macas' and the confirmed
    # (the May ones are out, whatever their payment date).
    assert matriz["orders"]["entered"] == 2
    assert other["orders"]["entered"] == 1


def test_an_order_is_finished_when_its_history_says_so(client, db_session):
    c = _client(db_session)
    # Created in May, closed in June: a June closing.
    _order(
        db_session,
        c.id,
        status="finished",
        created_at=_at(20, 10, month=5),
        history=[_finished(_at(5, 15))],
    )
    # Created in June, closed in July: not a June closing.
    _order(
        db_session,
        c.id,
        status="finished",
        created_at=_at(3, 10),
        history=[_finished(_at(2, 15, month=7))],
    )

    data = _get(client, "branch-comparison")
    assert data["branches"][0]["orders"] == {"entered": 1, "finished": 1}


def test_production_weighs_the_sheets_and_splits_the_workday(client, db_session):
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    _seed_cut_day(db_session, c.id, op)

    production = _get(client, "branch-comparison")["branches"][0]["production"]

    assert production["boards"] == 1.5  # whole 1 + half 0.5 + retazo 0
    assert production["cutLinearM"] == 5.38  # 3.66 + 1.22 + 0.50
    assert production["effectiveHours"] == 0.42  # 25 min
    assert production["pausedHours"] == 0.5  # the 30-min stop
    assert production["boardsPerHour"] == 3.6  # 1.5 / (25/60)
    assert production["metersPerHour"] == 12.91
    assert production["daysWorked"] == 1
    assert production["averageStartMinute"] == 7 * 60 + 55
    assert production["averageEndMinute"] == 8 * 60 + 50


def test_a_sheet_belongs_to_the_day_its_last_piece_was_cut(client, db_session):
    """Started on June 30th, closed on July 1st (local): a July sheet."""
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    order = _order(db_session, c.id, status="in_process")
    board = _board(db_session, order.id)
    _piece(db_session, order.id, board.id, _at(30, 16), op.id, n=1)
    _piece(db_session, order.id, board.id, _at(1, 8, month=7), op.id, n=2)

    june = _get(client, "branch-comparison")["total"]["production"]
    july = client.get(
        "/api/v1/analytics/branch-comparison",
        params={"from": "2026-07-01", "to": "2026-07-31"},
    ).json()["data"]["total"]["production"]

    assert june["boards"] == 0
    assert july["boards"] == 1.0


def test_the_total_recomputes_its_rates_from_its_sums(client, db_session):
    macas = _branch(db_session)
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    _seed_cut_day(db_session, c.id, op)
    order = _order(db_session, c.id, branch_id=macas.id, status="in_process")
    board = _board(db_session, order.id, cuts=(1000.0,))
    _piece(db_session, order.id, board.id, _at(10, 9, 0), op.id, n=1)
    _piece(db_session, order.id, board.id, _at(10, 9, 5), op.id, n=2)

    data = _get(client, "branch-comparison")
    total = data["total"]["production"]

    assert total["boards"] == 2.5
    assert total["effectiveHours"] == 0.5  # 25 + 5 minutes
    assert total["boardsPerHour"] == 5.0  # 2.5 / 0.5, not the mean of the two
    # Both branches cut on the 10th: one calendar day.
    assert total["daysWorked"] == 1


# ------------------------------------------------------------- production tab
def test_production_report_lists_days_stops_and_material(client, db_session):
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    _seed_cut_day(db_session, c.id, op)
    _order(
        db_session,
        c.id,
        status="finished",
        history=[_finished(_at(10, 16))],
        lines=[
            OrderLineModel(
                quantity=2,
                unit_price_snapshot=45.0,
                line_total=90.0,
                avg_efficiency=80.0,
                total_area_m2=10.0,
            )
        ],
    )

    data = _get(client, "production")

    [day] = data["days"]
    assert day["date"] == "2026-06-10"
    assert day["branchName"] == "Casa Matriz"
    assert day["firstEventAt"] == "2026-06-10T12:55:00Z"  # 07:55 local
    assert day["lastEventAt"] == "2026-06-10T13:50:00Z"
    assert day["effectiveHours"] == 0.42
    assert day["pausedHours"] == 0.5
    assert day["boards"] == 1.5
    assert day["ordersFinished"] == 1
    [stop] = data["stops"]
    assert stop["startedAt"] == "2026-06-10T13:10:00Z"  # 08:10 local
    assert stop["endedAt"] == "2026-06-10T13:40:00Z"
    assert stop["minutes"] == 30
    [material] = data["material"]
    assert material["averageEfficiency"] == 80.0
    assert material["areaCutM2"] == 10.0
    assert material["wasteEstimateM2"] == 2.0


def test_production_report_filters_by_branch(client, db_session):
    macas = _branch(db_session)
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    _seed_cut_day(db_session, c.id, op)

    data = _get(client, "production", branchId=macas.id)
    assert data["days"] == [] and data["stops"] == []


# -------------------------------------------------------------- by role
def test_sellers_are_credited_with_their_quotes_sales_and_pending(client, db_session):
    c = _client(db_session)
    ana = _user(db_session, "Ana", ["vendedor"])
    luis = _user(db_session, "Luis", ["vendedor"])
    _order(db_session, c.id, queued_at=_at(5, 10), cash=60.0, created_by=ana.id)
    _order(db_session, c.id, queued_at=_at(6, 10), credit=40.0, created_by=ana.id)
    _order(db_session, c.id, queued_at=_at(7, 10), transfer=30.0, created_by=luis.id)
    # Confirmed, not yet paid: Luis still has to collect it.
    _order(db_session, c.id, status="confirmed", total=75.0, created_by=luis.id)

    data = _get(client, "productivity/sellers")

    first, second = data["sellers"]
    assert first["fullName"] == "Ana"
    assert first["branchName"] == "Casa Matriz"
    assert (first["cash"], first["credit"], first["total"]) == (60.0, 40.0, 100.0)
    assert first["averageTicket"] == 50.0
    assert first["pendingCount"] == 0
    assert second["fullName"] == "Luis"
    assert second["cash"] == 30.0  # the transfer is cash in hand
    assert (second["pendingCount"], second["pendingAmount"]) == (1, 75.0)
    assert data["total"]["total"] == 130.0
    assert data["total"]["paidOrders"] == 3
    assert data["total"]["pendingAmount"] == 75.0


def test_a_sheet_is_credited_to_who_cut_its_last_piece(client, db_session):
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    other = _user(db_session, "Op Dos", ["operador"])
    _seed_cut_day(db_session, c.id, op, closer=other)

    data = _get(client, "productivity/operators")
    rows = {r["fullName"]: r for r in data["operators"]}

    assert rows["Op Uno"]["boards"] == 1.0  # the whole one; the retazo weighs 0
    assert rows["Op Uno"]["piecesCut"] == 5
    assert rows["Op Dos"]["boards"] == 0.5  # closed the half
    assert rows["Op Dos"]["piecesCut"] == 1
    assert rows["Op Dos"]["effectiveHours"] == 0  # a lone mark is no workday
    assert data["total"]["boards"] == 1.5
    assert data["total"]["ordersCut"] == 1


def test_banders_count_what_they_closed_in_range(client, db_session):
    c = _client(db_session)
    bander = _user(db_session, "Can Uno", ["canteador"])
    order = _order(db_session, c.id, status="finished", banding_m=27.23)
    _activity(
        db_session,
        order.id,
        "banding",
        started_at=_at(10, 9),
        finished_at=_at(10, 11),
        finished_by=bander.id,
    )
    _activity(
        db_session,
        order.id,
        "additional",
        started_at=_at(10, 11),
        finished_at=_at(10, 12),
        finished_by=bander.id,
    )
    late = _order(db_session, c.id, status="finished", banding_m=99.0)
    _activity(
        db_session,
        late.id,
        "banding",
        started_at=_at(30, 17),
        finished_at=_at(1, 9, month=7),
        finished_by=bander.id,
    )

    [row] = _get(client, "productivity/banders")["banders"]
    assert row["ordersBanded"] == 1
    assert row["bandingHours"] == 2.0
    assert row["averageBandingHours"] == 2.0
    # The net tape of the order banded in range; July's closing stays out.
    assert row["bandedLinearM"] == 27.23
    assert row["bandingMetersPerHour"] == 13.62  # 27.23 m over 2 h
    assert row["ordersAdditional"] == 1
    assert row["additionalHours"] == 1.0


def test_banded_metres_count_for_the_branch_on_the_day_the_banding_closed(
    client, db_session
):
    """Net tape of each banding closed in range: in the comparison and on its day.

    The additional work carries no tape, and a banding closed in July stays out
    of June whatever day it started.
    """
    macas = _branch(db_session)
    c = _client(db_session)
    bander = _user(db_session, "Can Uno", ["canteador"])
    june = _order(db_session, c.id, status="finished", banding_m=27.23)
    _activity(
        db_session,
        june.id,
        "banding",
        started_at=_at(10, 9),
        finished_at=_at(10, 11),
        finished_by=bander.id,
    )
    _activity(
        db_session,
        june.id,
        "additional",
        started_at=_at(10, 11),
        finished_at=_at(10, 12),
        finished_by=bander.id,
    )
    other = _order(
        db_session, c.id, branch_id=macas.id, status="finished", banding_m=12.5
    )
    _activity(db_session, other.id, "banding", finished_at=_at(11, 15))
    late = _order(db_session, c.id, status="finished", banding_m=99.0)
    _activity(
        db_session,
        late.id,
        "banding",
        started_at=_at(30, 17),
        finished_at=_at(1, 9, month=7),
    )

    data = _get(client, "branch-comparison")
    matriz, norte = data["branches"]
    assert matriz["production"]["bandedLinearM"] == 27.23
    assert norte["production"]["bandedLinearM"] == 12.5
    assert data["total"]["production"]["bandedLinearM"] == 39.73

    days = {(d["date"], d["branchName"]): d for d in _get(client, "production")["days"]}
    assert days[("2026-06-10", "Casa Matriz")]["bandedLinearM"] == 27.23
    assert days[("2026-06-11", "Macas")]["bandedLinearM"] == 12.5


def test_banders_time_only_the_clocked_work(client, db_session):
    """A start and a close registered together count as work, not as time.

    The 2-h banding is clocked; the one closed 10 s after its start was tapped
    in after the work: its order and its metres count, its seconds do not, and
    its metres stay out of the metres per hour.
    """
    c = _client(db_session)
    bander = _user(db_session, "Can Uno", ["canteador"])
    clocked = _order(db_session, c.id, status="finished", banding_m=27.23)
    _activity(
        db_session,
        clocked.id,
        "banding",
        started_at=_at(10, 9),
        finished_at=_at(10, 11),
        finished_by=bander.id,
    )
    tapped = _order(db_session, c.id, status="finished", banding_m=15.0)
    _activity(
        db_session,
        tapped.id,
        "banding",
        started_at=_at(11, 16, 0),
        finished_at=_at(11, 16, 0) + timedelta(seconds=10),
        finished_by=bander.id,
    )
    _activity(
        db_session,
        tapped.id,
        "additional",
        started_at=_at(11, 16, 1),
        finished_at=_at(11, 16, 1) + timedelta(seconds=5),
        finished_by=bander.id,
    )

    data = _get(client, "productivity/banders")
    [row] = data["banders"]
    assert row["ordersBanded"] == 2
    assert row["ordersBandedUnclocked"] == 1
    assert row["bandedLinearM"] == 42.23  # every metre laid counts
    assert row["bandingHours"] == 2.0
    assert row["averageBandingHours"] == 2.0  # over the one clocked
    assert row["bandingMetersPerHour"] == 13.62  # 27.23 m over 2 h, not 42.23
    assert row["ordersAdditional"] == 1
    assert row["ordersAdditionalUnclocked"] == 1
    assert row["additionalHours"] == 0
    assert row["averageAdditionalHours"] == 0
    assert data["total"]["ordersBandedUnclocked"] == 1


def test_bandings_and_additional_works_are_counted_by_branch_and_day(
    client, db_session
):
    macas = _branch(db_session)
    c = _client(db_session)
    june = _order(db_session, c.id, status="finished", banding_m=10.0)
    _activity(db_session, june.id, "banding", finished_at=_at(10, 11))
    _activity(db_session, june.id, "additional", finished_at=_at(10, 12))
    other = _order(db_session, c.id, branch_id=macas.id, status="finished")
    _activity(db_session, other.id, "additional", finished_at=_at(11, 15))
    # Still open: no close, nothing counted.
    _activity(
        db_session,
        _order(db_session, c.id, status="in_process").id,
        "banding",
        status="in_progress",
        started_at=_at(11, 9),
    )

    data = _get(client, "branch-comparison")
    matriz, norte = data["branches"]
    assert (
        matriz["production"]["ordersBanded"],
        norte["production"]["ordersBanded"],
    ) == (1, 0)
    assert (
        matriz["production"]["ordersAdditional"],
        norte["production"]["ordersAdditional"],
    ) == (1, 1)
    assert data["total"]["production"]["ordersAdditional"] == 2

    days = {(d["date"], d["branchName"]): d for d in _get(client, "production")["days"]}
    assert days[("2026-06-10", "Casa Matriz")]["ordersBanded"] == 1
    assert days[("2026-06-10", "Casa Matriz")]["ordersAdditional"] == 1
    assert days[("2026-06-11", "Macas")]["ordersAdditional"] == 1
    assert days[("2026-06-11", "Macas")]["ordersBanded"] == 0


# ------------------------------------------------------------ the board ledger
def test_a_deleted_users_sheets_stay_in_a_row_of_their_own(client, db_session):
    """``cut_by`` goes NULL when the user is deleted; the sheet still counts.

    Without the row the operators' total would fall short of the comparison's.
    """
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    _seed_cut_day(db_session, c.id, op)
    order = _order(db_session, c.id, status="in_process")
    board = _board(db_session, order.id)
    _piece(db_session, order.id, board.id, _at(12, 9, 0), None, n=1)
    _piece(db_session, order.id, board.id, _at(12, 9, 5), None, n=2)

    data = _get(client, "productivity/operators")

    *named, gone = data["operators"]
    assert [r["fullName"] for r in named] == ["Op Uno"]
    assert gone["userId"] is None and gone["fullName"] == "Sin usuario"
    assert (gone["boards"], gone["piecesCut"]) == (1.0, 2)
    comparison = _get(client, "branch-comparison")["total"]["production"]
    assert data["total"]["boards"] == comparison["boards"] == 2.5


def test_the_ledger_adds_up_to_the_operators_row(client, db_session):
    c = _client(db_session)
    op = _user(db_session, "Op Uno", ["operador"])
    other = _user(db_session, "Op Dos", ["operador"])
    order = _seed_cut_day(db_session, c.id, op, closer=other)
    order.code = "ORD-000042"
    # A sheet they started on June 30th and closed on July 1st (local).
    late = _board(db_session, order.id)
    _piece(db_session, order.id, late.id, _at(30, 16), op.id, n=1)
    _piece(db_session, order.id, late.id, _at(1, 8, month=7), op.id, n=2)
    for user in (op, other):
        db_session.query(OrderPlacedPieceModel).filter(
            OrderPlacedPieceModel.cut_by == user.id
        ).update({"cut_by_label": user.full_name})
    db_session.commit()

    [row] = [
        r
        for r in _get(client, "productivity/operators")["operators"]
        if r["userId"] == op.id
    ]
    ledger = _get(client, f"productivity/operators/{op.id}/boards")

    assert ledger["fullName"] == "Op Uno"
    assert ledger["boards"] == row["boards"] == 1.0
    assert ledger["piecesCut"] == row["piecesCut"] == 6
    assert ledger["creditedCount"] == 2  # the whole board and the retazo
    by_status = defaultdict(list)
    for sheet in ledger["sheets"]:
        by_status[sheet["status"]].append(sheet)

    whole, retazo = sorted(by_status["credited"], key=lambda s: s["weight"])[::-1]
    assert (whole["kind"], whole["weight"]) == ("whole", 1.0)
    assert (retazo["kind"], retazo["weight"]) == ("offcut", 0.0)
    assert whole["orderCode"] == "ORD-000042"
    assert whole["clientName"] == "0100000397"
    assert whole["day"] == "2026-06-10"
    assert whole["piecesMine"] == whole["piecesTotal"] == 2

    [half] = by_status["credited_to_other"]
    assert (half["kind"], half["weight"]) == ("half", 0.5)
    assert half["closedBy"] == "Op Dos"
    assert half["otherCutters"] == ["Op Dos"]
    assert (half["piecesMine"], half["piecesByOthers"]) == (1, 1)

    [pending] = by_status["incomplete"]
    assert (pending["piecesPending"], pending["doneAt"]) == (1, None)

    [straddling] = by_status["outside_range"]
    assert straddling["doneAt"] == "2026-07-01T13:00:00Z"
    assert (straddling["piecesMine"], straddling["piecesMineInRange"]) == (2, 1)
    # Newest first.
    assert ledger["sheets"][0]["boardId"] == straddling["boardId"]


def test_the_ledger_of_an_unknown_user_is_a_404(client):
    resp = client.get("/api/v1/analytics/productivity/operators/9999/boards")
    assert resp.status_code == 404


def test_the_ledger_is_admin_only(client, db_session):
    from tests.order_helpers import _token_for

    op = _user(db_session, "Op Uno", ["operador"])
    seller = _token_for(client, db_session, "vendedor")
    resp = client.get(
        f"/api/v1/analytics/productivity/operators/{op.id}/boards", headers=seller
    )
    assert resp.status_code == 403


# ------------------------------------------------- the sellers' and banders' detail
def test_a_sellers_orders_add_up_to_their_row(client, db_session):
    c = _client(db_session)
    ana = _user(db_session, "Ana", ["vendedor"])
    luis = _user(db_session, "Luis", ["vendedor"])
    # Confirmed in May, paid in June: a June sale, and the detail says both dates.
    may = _order(
        db_session,
        c.id,
        created_at=_at(28, 10, month=5),
        queued_at=_at(5, 10),
        cash=50.0,
        transfer=10.0,
        credit=40.0,
        created_by=ana.id,
        code="ORD-000050",
    )
    may.external_invoice_id = "001-002-000123"
    _order(
        db_session, c.id, queued_at=_at(6, 10), cash=20.0, created_by=ana.id, code="B"
    )
    # Not hers, cancelled, or paid in July: not in her detail.
    _order(db_session, c.id, queued_at=_at(6, 11), cash=99.0, created_by=luis.id)
    _order(
        db_session,
        c.id,
        status="cancelled",
        queued_at=_at(7, 10),
        cash=99.0,
        created_by=ana.id,
    )
    _order(
        db_session, c.id, queued_at=_at(1, 10, month=7), cash=99.0, created_by=ana.id
    )
    # Still to collect today.
    _order(
        db_session, c.id, status="confirmed", total=75.0, created_by=ana.id, code="P"
    )
    db_session.commit()

    [row] = [
        r
        for r in _get(client, "productivity/sellers")["sellers"]
        if r["userId"] == ana.id
    ]
    detail = _get(client, f"productivity/sellers/{ana.id}/orders")

    figures = {k: row[k] for k in detail["figures"]}
    assert detail["figures"] == figures
    assert (figures["total"], figures["paidOrders"], figures["pendingAmount"]) == (
        120.0,
        2,
        75.0,
    )
    first, second = detail["paid"]  # newest payment first
    assert second["orderCode"] == "ORD-000050"
    assert second["createdAt"].startswith("2026-05-28")
    assert second["day"] == "2026-06-05"
    assert (second["cash"], second["transfer"], second["credit"]) == (50.0, 10.0, 40.0)
    assert second["total"] == 100.0
    assert second["invoice"] == "001-002-000123"
    assert second["clientName"] == "0100000397"
    assert first["invoice"] is None
    [pending] = detail["pending"]
    assert (pending["orderCode"], pending["total"]) == ("P", 75.0)


def test_a_banders_orders_add_up_to_their_row(client, db_session):
    c = _client(db_session)
    bander = _user(db_session, "Can Uno", ["canteador"])
    other = _user(db_session, "Can Dos", ["canteador"])
    clocked = _order(
        db_session, c.id, status="finished", banding_m=27.23, code="ORD-000070"
    )
    _activity(
        db_session,
        clocked.id,
        "banding",
        started_at=_at(10, 9),
        finished_at=_at(10, 11),
        finished_by=bander.id,
    )
    tapped = _order(db_session, c.id, status="finished", banding_m=15.0, code="T")
    _activity(
        db_session,
        tapped.id,
        "banding",
        started_at=_at(11, 16),
        finished_at=_at(11, 16) + timedelta(seconds=8),
        finished_by=bander.id,
    )
    _activity(
        db_session,
        tapped.id,
        "additional",
        started_at=_at(11, 17),
        finished_at=_at(11, 17, 30),
        finished_by=bander.id,
    )
    # Somebody else's.
    _activity(
        db_session,
        _order(db_session, c.id, status="finished", banding_m=5.0).id,
        "banding",
        started_at=_at(12, 9),
        finished_at=_at(12, 10),
        finished_by=other.id,
    )

    [row] = [
        r
        for r in _get(client, "productivity/banders")["banders"]
        if r["userId"] == bander.id
    ]
    detail = _get(client, f"productivity/banders/{bander.id}/orders")

    assert detail["figures"] == {k: row[k] for k in detail["figures"]}
    assert detail["fullName"] == "Can Uno"
    additional, tapped_banding, clocked_banding = detail["orders"]  # newest close first
    assert (additional["kind"], additional["hours"]) == ("additional", 0.5)
    assert additional["bandedLinearM"] == 0
    assert (tapped_banding["hours"], tapped_banding["bandedLinearM"]) == (None, 15.0)
    assert clocked_banding["orderCode"] == "ORD-000070"
    assert (clocked_banding["hours"], clocked_banding["day"]) == (2.0, "2026-06-10")


def test_the_detail_of_an_unknown_user_is_a_404(client):
    for team in ("sellers", "banders"):
        resp = client.get(f"/api/v1/analytics/productivity/{team}/9999/orders")
        assert resp.status_code == 404, team
