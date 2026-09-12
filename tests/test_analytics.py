"""Tests for the analytics module: summary, timeseries, status breakdown, and operations.

Seeding goes straight through ``db_session`` to pin statuses, ``created_at``, and history
precisely (the state machine doesn't let every case be reached cleanly).
"""

from datetime import datetime

from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    ActivityType,
    OrderActivityModel,
    OrderBoardModel,
    OrderLineModel,
    OrderModel,
    OrderPlacedPieceModel,
    OrderStatusHistoryModel,
)
from src.modules.users.login_event_model import UserLoginEventModel
from src.modules.users.model import UserModel

_BASE = datetime(2026, 6, 15, 12, 0, 0)
_RANGE = {"from": "2026-06-01", "to": "2026-06-30"}
_CUT_AT = datetime(2026, 6, 15, 10, 0, 0)


def _board_line(efficiency, area, *, qty=2, price=45.5):
    return OrderLineModel(
        product_id=None,
        quantity=qty,
        unit_price_snapshot=price,
        line_total=qty * price,
        avg_efficiency=efficiency,
        total_area_m2=area,
    )


def _edge_line(linear_m, *, price=1.5):
    """Edge-banding line: no area/efficiency (must not contaminate the weighting)."""
    return OrderLineModel(
        product_id=None,
        quantity=int(linear_m),
        unit_price_snapshot=price,
        line_total=linear_m * price,
        linear_m=linear_m,
    )


def _seed_activity(
    order_id,
    activity_type,
    *,
    status="done",
    started_at=None,
    finished_at=None,
    finished_by=None,
):
    """One activity row: where the work's own duration and actor live now.

    Before the activities only the banding had columns, so the cut had to be
    reconstructed from a pair of history rows -- which is also why the legacy
    pairs are still mapped in ``STATUS_PAIR_TO_STAGE``.
    """
    return OrderActivityModel(
        order_id=order_id,
        type=activity_type.value,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        finished_by=finished_by,
    )


def _seed_order(
    db,
    *,
    client_id=1,
    status="finished",
    total=100.0,
    boards=2,
    created_at=_BASE,
    lines=None,
    history=None,
    optimization_hash="h",
    branch_id=1,
):
    order = OrderModel(
        client_id=client_id,
        branch_id=branch_id,
        status=status,
        optimization_snapshot={},
        optimization_hash=optimization_hash,
        currency="USD",
        subtotal=total,
        total=total,
        total_boards_used=boards,
        created_at=created_at,
        confirmed_at=created_at,
    )
    if lines is not None:
        order.lines = lines
    if history is not None:
        order.history = history
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _seed_clients(db, n=1):
    """Seed n clients with unique identifiers; the first one gets id=1, and so on."""
    clients = [ClientModel(identifier=f"TEST{i:07d}") for i in range(1, n + 1)]
    for c in clients:
        db.add(c)
    db.commit()
    return clients


def _hist(to_status, created_at, from_status=None):
    return OrderStatusHistoryModel(
        from_status=from_status,
        to_status=to_status,
        actor="system",
        created_at=created_at,
    )


def _seed_user(db, *, role="operador", full_name="User", branch_id=1, email=None):
    user = UserModel(
        email=email or f"{full_name.replace(' ', '').lower()}@e.com",
        full_name=full_name,
        hashed_password="x",
        role=role,
        branch_id=branch_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_board(db, order_id):
    board = OrderBoardModel(
        order_id=order_id,
        sheet_number=1,
        material_key="m",
        width=2440,
        height=1220,
        thickness=15,
    )
    db.add(board)
    db.commit()
    db.refresh(board)
    return board


def _seed_placed_piece(
    db,
    *,
    order_id,
    board_id,
    cut_by,
    cut_at=_CUT_AT,
    width=600,
    height=400,
    piece_id="p#1",
):
    db.add(
        OrderPlacedPieceModel(
            order_id=order_id,
            board_id=board_id,
            piece_id=piece_id,
            label="p",
            x=0,
            y=0,
            width=width,
            height=height,
            original_width=width,
            original_height=height,
            rotated=False,
            cut_at=cut_at,
            cut_by=cut_by,
            cut_by_label="Op",
        )
    )
    db.commit()


def _seed_login(db, user_id, created_at):
    db.add(UserLoginEventModel(user_id=user_id, created_at=created_at))
    db.commit()


# --------------------------------------------------------------------- summary
def test_summary_empty_range_returns_zeros_not_nulls(client):
    data = client.get("/api/v1/analytics/summary", params=_RANGE).json()["data"]

    for key in (
        "totalBoardsConsumed",
        "averageEfficiency",
        "totalAreaCutM2",
        "wasteEstimateM2",
        "pendingOrdersCount",
        "cancellationRate",
        "orderCount",
        "realizedRevenue",
        "averageTicket",
        "activeClientsCount",
    ):
        assert data[key] == 0, key
        assert data[key] is not None
    assert data["range"]["dateFrom"] == "2026-06-01"
    assert data["range"]["dateTo"] == "2026-06-30"


def test_summary_revenue_and_rates_isolated_by_status(client, db_session):
    _seed_clients(db_session, 2)
    _seed_order(db_session, client_id=1, status="finished", total=100.0)
    _seed_order(db_session, client_id=2, status="confirmed", total=200.0)
    _seed_order(db_session, client_id=1, status="cancelled", total=50.0)

    data = client.get("/api/v1/analytics/summary", params=_RANGE).json()["data"]

    assert data["orderCount"] == 3
    assert data["realizedRevenue"] == 100.0  # finished only
    assert data["averageTicket"] == 100.0  # 100 / 1 finished order
    assert data["pendingOrdersCount"] == 1  # confirmed
    assert data["cancellationRate"] == round(1 / 3, 4)  # 1 / 3
    assert data["activeClientsCount"] == 2  # clients 1 and 2 are distinct


def test_summary_efficiency_is_area_weighted_and_ignores_edge_banding(
    client, db_session
):
    _seed_clients(db_session)
    _seed_order(
        db_session,
        status="finished",
        boards=2,
        lines=[_board_line(90.0, 10.0), _edge_line(5.0)],
    )
    _seed_order(
        db_session, status="finished", boards=5, lines=[_board_line(70.0, 30.0)]
    )

    data = client.get("/api/v1/analytics/summary", params=_RANGE).json()["data"]

    # Weighted: (90*10 + 70*30) / (10+30) = 3000/40 = 75.0
    assert data["averageEfficiency"] == 75.0
    assert data["totalAreaCutM2"] == 40.0
    assert data["wasteEstimateM2"] == 10.0  # 40 * (1 - 0.75)
    assert data["totalBoardsConsumed"] == 7  # 2 + 5


def test_summary_uses_default_range_when_omitted(client):
    data = client.get("/api/v1/analytics/summary").json()["data"]
    assert data["range"]["dateFrom"] < data["range"]["dateTo"]


def test_invalid_range_returns_422_with_envelope(client):
    resp = client.get(
        "/api/v1/analytics/summary", params={"from": "2026-06-30", "to": "2026-06-01"}
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "from"


# ------------------------------------------------------------- date filtering
def test_date_filter_is_half_open(client, db_session):
    _seed_clients(db_session)
    _seed_order(db_session, total=100.0, created_at=datetime(2026, 6, 1, 0, 0, 0))
    _seed_order(db_session, total=50.0, created_at=datetime(2026, 6, 10, 23, 0, 0))
    _seed_order(db_session, total=999.0, created_at=datetime(2026, 5, 31, 23, 0, 0))
    _seed_order(db_session, total=999.0, created_at=datetime(2026, 6, 11, 0, 0, 0))

    data = client.get(
        "/api/v1/analytics/summary", params={"from": "2026-06-01", "to": "2026-06-10"}
    ).json()["data"]
    assert data["orderCount"] == 2
    assert data["realizedRevenue"] == 150.0


# ------------------------------------------------------------------ timeseries
def test_timeseries_daily_dense_axis_with_zero_gaps(client, db_session):
    _seed_clients(db_session)
    _seed_order(
        db_session, total=100.0, boards=2, created_at=datetime(2026, 6, 1, 9, 0)
    )
    _seed_order(db_session, total=50.0, boards=1, created_at=datetime(2026, 6, 3, 9, 0))

    data = client.get(
        "/api/v1/analytics/timeseries",
        params={"from": "2026-06-01", "to": "2026-06-03", "granularity": "day"},
    ).json()["data"]

    assert data["granularity"] == "day"
    assert data["buckets"] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert data["series"]["revenue"] == [100.0, 0.0, 50.0]
    assert data["series"]["orderCount"] == [1, 0, 1]
    assert data["series"]["boardsConsumed"] == [2, 0, 1]


def test_timeseries_monthly_buckets(client, db_session):
    _seed_clients(db_session)
    _seed_order(db_session, total=100.0, created_at=datetime(2026, 6, 20, 9, 0))

    data = client.get(
        "/api/v1/analytics/timeseries",
        params={"from": "2026-05-15", "to": "2026-07-10", "granularity": "month"},
    ).json()["data"]

    assert data["buckets"] == ["2026-05-01", "2026-06-01", "2026-07-01"]
    assert data["series"]["revenue"] == [0.0, 100.0, 0.0]


def test_timeseries_monthly_crosses_year_boundary(client, db_session):
    _seed_clients(db_session)
    _seed_order(db_session, total=100.0, created_at=datetime(2026, 1, 10, 9, 0))

    data = client.get(
        "/api/v1/analytics/timeseries",
        params={"from": "2025-12-01", "to": "2026-01-31", "granularity": "month"},
    ).json()["data"]

    assert data["buckets"] == ["2025-12-01", "2026-01-01"]
    assert data["series"]["revenue"] == [0.0, 100.0]


def test_timeseries_weekly_buckets(client, db_session):
    _seed_clients(db_session)
    # 2026-06-01 is a Monday; ISO weeks start on 06-01 and 06-08.
    _seed_order(db_session, total=100.0, created_at=datetime(2026, 6, 3, 9, 0))
    _seed_order(db_session, total=50.0, created_at=datetime(2026, 6, 10, 9, 0))

    data = client.get(
        "/api/v1/analytics/timeseries",
        params={"from": "2026-06-01", "to": "2026-06-14", "granularity": "week"},
    ).json()["data"]

    assert data["buckets"] == ["2026-06-01", "2026-06-08"]
    assert data["series"]["revenue"] == [100.0, 50.0]


def test_timeseries_new_clients_counts_first_order_only(client, db_session):
    _seed_clients(db_session, 3)
    # Client 1: first order on 06-01, second on 06-02 (doesn't recount).
    _seed_order(db_session, client_id=1, created_at=datetime(2026, 6, 1, 9, 0))
    _seed_order(db_session, client_id=1, created_at=datetime(2026, 6, 2, 9, 0))
    # Client 2: new on 06-02.
    _seed_order(db_session, client_id=2, created_at=datetime(2026, 6, 2, 9, 0))
    # Client 3: their first order was before the range, so not "new" here.
    _seed_order(db_session, client_id=3, created_at=datetime(2026, 5, 1, 9, 0))
    _seed_order(db_session, client_id=3, created_at=datetime(2026, 6, 2, 9, 0))

    data = client.get(
        "/api/v1/analytics/timeseries",
        params={"from": "2026-06-01", "to": "2026-06-03", "granularity": "day"},
    ).json()["data"]

    assert data["series"]["newClients"] == [1, 1, 0]


# ------------------------------------------------------------- breakdown/status
def test_breakdown_status_densifies_all_states(client, db_session):
    _seed_clients(db_session)
    _seed_order(db_session, status="finished", total=100.0)
    _seed_order(db_session, status="finished", total=200.0)
    _seed_order(db_session, status="cancelled", total=50.0)

    data = client.get("/api/v1/analytics/breakdown/status", params=_RANGE).json()[
        "data"
    ]

    assert data["dimension"] == "status"
    assert len(data["items"]) == 6  # every LIVE status (the two legacy ones are out)
    by_key = {it["key"]: it for it in data["items"]}
    assert by_key["finished"]["orderCount"] == 2
    assert by_key["finished"]["revenue"] == 300.0
    assert by_key["finished"]["label"] == "Terminada"
    assert by_key["cancelled"]["orderCount"] == 1
    assert by_key["cancelled"]["revenue"] == 50.0
    assert by_key["confirmed"]["orderCount"] == 0  # densified to zero


def test_breakdown_status_empty_range(client):
    data = client.get("/api/v1/analytics/breakdown/status", params=_RANGE).json()[
        "data"
    ]
    assert len(data["items"]) == 6
    assert all(it["orderCount"] == 0 and it["revenue"] == 0 for it in data["items"])


# ------------------------------------------------------------------ operations
def test_operations_efficiency_mirrors_summary(client, db_session):
    _seed_clients(db_session)
    _seed_order(db_session, status="finished", lines=[_board_line(90.0, 10.0)])
    _seed_order(db_session, status="finished", lines=[_board_line(70.0, 30.0)])

    data = client.get("/api/v1/analytics/operations", params=_RANGE).json()["data"]
    assert data["averageEfficiency"] == 75.0
    assert data["totalAreaCutM2"] == 40.0
    assert data["wasteEstimateM2"] == 10.0


def test_operations_empty_range(client):
    data = client.get("/api/v1/analytics/operations", params=_RANGE).json()["data"]
    assert data["averageEfficiency"] == 0
    assert data["totalAreaCutM2"] == 0
    assert data["wasteEstimateM2"] == 0
    assert "lifecycle" not in data  # lifecycle lives in /bottlenecks


# ------------------------------------------------------------------ bottlenecks
def test_bottlenecks_stage_durations_and_slowest_first(client, db_session):
    _seed_clients(db_session)
    # Queue wait = 1h; the cut = 6h (the bottleneck), from its activity row.
    history = [
        _hist("confirmed", datetime(2026, 6, 15, 0, 0)),
        _hist("queued", datetime(2026, 6, 15, 1, 0), from_status="confirmed"),
        _hist("in_process", datetime(2026, 6, 15, 2, 0), from_status="queued"),
    ]
    order = _seed_order(db_session, status="in_process", history=history)
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.cutting,
            started_at=datetime(2026, 6, 15, 2, 0),
            finished_at=datetime(2026, 6, 15, 8, 0),
        )
    )
    db_session.commit()

    data = client.get("/api/v1/analytics/bottlenecks", params=_RANGE).json()["data"]
    stages = {s["key"]: s for s in data["stages"]}
    assert len(data["stages"]) == 7  # the 7 densified stages
    assert stages["queue_wait"]["avgHours"] == 1.0
    assert stages["queue_wait"]["sampleCount"] == 1
    assert stages["cutting"]["avgHours"] == 6.0
    assert stages["cutting"]["medianHours"] == 6.0
    assert stages["cutting"]["p90Hours"] == 6.0
    assert stages["cutting"]["label"] == "Corte"
    # Sorted by median desc: cutting (6h) comes before queue wait (1h).
    keys = [s["key"] for s in data["stages"]]
    assert keys.index("cutting") < keys.index("queue_wait")
    # Stages with no samples stay at zero (densified).
    assert stages["dispatch_wait"]["sampleCount"] == 0
    assert stages["dispatch_wait"]["avgHours"] == 0.0


def test_bottlenecks_banding_stage_from_its_activity(client, db_session):
    _seed_clients(db_session)
    order = _seed_order(db_session, status="in_process")
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.banding,
            started_at=datetime(2026, 6, 15, 10, 0),
            finished_at=datetime(2026, 6, 15, 13, 0),  # 3h of edge banding
        )
    )
    db_session.commit()

    data = client.get("/api/v1/analytics/bottlenecks", params=_RANGE).json()["data"]
    banding = next(s for s in data["stages"] if s["key"] == "banding")
    assert banding["avgHours"] == 3.0
    assert banding["sampleCount"] == 1


def test_bottlenecks_median_and_p90_across_orders(client, db_session):
    _seed_clients(db_session)
    # Three cuts of 2h, 4h, and 10h -> median 4h, high p90 (slow tail).
    for end_hour in (2, 4, 10):
        history = [
            _hist("in_process", datetime(2026, 6, 15, 0, 0)),
        ]
        order = _seed_order(db_session, status="in_process", history=history)
        db_session.add(
            _seed_activity(
                order.id,
                ActivityType.cutting,
                started_at=datetime(2026, 6, 15, 0, 0),
                finished_at=datetime(2026, 6, 15, end_hour, 0),
            )
        )
        db_session.commit()

    data = client.get("/api/v1/analytics/bottlenecks", params=_RANGE).json()["data"]
    cutting = next(s for s in data["stages"] if s["key"] == "cutting")
    assert cutting["sampleCount"] == 3
    assert cutting["medianHours"] == 4.0
    assert cutting["p90Hours"] > 4.0  # the p90 exposes the 10h order


def test_bottlenecks_series_places_duration_in_bucket(client, db_session):
    _seed_clients(db_session)
    history = [
        _hist("cutting", datetime(2026, 6, 2, 0, 0)),
        _hist("cut", datetime(2026, 6, 2, 3, 0), from_status="cutting"),
    ]
    _seed_order(
        db_session, status="cut", created_at=datetime(2026, 6, 1, 9, 0), history=history
    )

    data = client.get(
        "/api/v1/analytics/bottlenecks",
        params={"from": "2026-06-01", "to": "2026-06-03", "granularity": "day"},
    ).json()["data"]
    assert data["buckets"] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    cutting = next(s for s in data["series"] if s["key"] == "cutting")
    # Cutting closes on 06-02, so its duration falls in that bucket.
    assert cutting["avgHours"] == [0.0, 3.0, 0.0]


# ----------------------------------------------------------- user productivity
def test_user_productivity_operator_cutting(client, db_session):
    _seed_clients(db_session)
    op = _seed_user(db_session, role="operador", full_name="Op Uno")
    order = _seed_order(db_session, status="in_process")
    order.assigned_to_id = op.id
    # The cut's hours and its boards are credited to whoever FINISHED it.
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.cutting,
            started_at=datetime(2026, 6, 15, 8, 0),
            finished_at=datetime(2026, 6, 15, 10, 0),
            finished_by=op.id,
        )
    )
    db_session.commit()
    board = _seed_board(db_session, order.id)
    _seed_placed_piece(db_session, order_id=order.id, board_id=board.id, cut_by=op.id)
    _seed_placed_piece(
        db_session,
        order_id=order.id,
        board_id=board.id,
        cut_by=op.id,
        piece_id="p#2",
    )

    data = client.get("/api/v1/analytics/users", params=_RANGE).json()["data"]
    row = next(u for u in data["users"] if u["userId"] == op.id)
    assert row["role"] == "operador"
    assert row["piecesCut"] == 2
    assert row["areaCutM2"] == 0.48  # 2 pieces of 600x400mm = 0.24 m² each
    assert row["ordersCut"] == 1
    assert row["cuttingHours"] == 2.0
    assert row["piecesPerHour"] == 1.0  # 2 pieces / 2h
    assert row["boardsCut"] == 2  # total_boards_used from _seed_order default


def test_user_productivity_seller_and_bander(client, db_session):
    _seed_clients(db_session)
    seller = _seed_user(db_session, role="vendedor", full_name="Vende")
    bander = _seed_user(db_session, role="canteador", full_name="Canta")
    order = _seed_order(db_session, status="finished", total=250.0)
    order.created_by = seller.id
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.banding,
            started_at=datetime(2026, 6, 15, 9, 0),
            finished_at=datetime(2026, 6, 15, 10, 0),  # 1h
            finished_by=bander.id,
        )
    )
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.additional,
            started_at=datetime(2026, 6, 15, 10, 0),
            finished_at=datetime(2026, 6, 15, 10, 30),  # 30 min
            finished_by=bander.id,
        )
    )
    db_session.commit()

    data = client.get("/api/v1/analytics/users", params=_RANGE).json()["data"]
    by_id = {u["userId"]: u for u in data["users"]}
    assert by_id[seller.id]["ordersCreated"] == 1
    assert by_id[seller.id]["revenueGenerated"] == 250.0
    assert by_id[bander.id]["ordersBanded"] == 1
    assert by_id[bander.id]["bandingHours"] == 1.0
    # The additional work is the canteador's too, and counted apart.
    assert by_id[bander.id]["ordersAdditional"] == 1
    assert by_id[bander.id]["additionalHours"] == 0.5


def test_user_productivity_filters_by_role(client, db_session):
    _seed_clients(db_session)
    op = _seed_user(db_session, role="operador", full_name="Op")
    seller = _seed_user(db_session, role="vendedor", full_name="Vende")
    o1 = _seed_order(db_session, status="finished")
    o1.created_by = seller.id
    o2 = _seed_order(db_session, status="cut")
    o2.assigned_to_id = op.id
    db_session.commit()
    board = _seed_board(db_session, o2.id)
    _seed_placed_piece(db_session, order_id=o2.id, board_id=board.id, cut_by=op.id)

    data = client.get(
        "/api/v1/analytics/users", params={**_RANGE, "role": "operador"}
    ).json()["data"]
    assert [u["userId"] for u in data["users"]] == [op.id]


# --------------------------------------------------------------------- attendance
def test_attendance_first_login_per_day(client, db_session):
    op = _seed_user(db_session, role="operador", full_name="Op Uno")
    _seed_login(db_session, op.id, datetime(2026, 6, 15, 8, 5))
    _seed_login(db_session, op.id, datetime(2026, 6, 15, 13, 30))  # same day
    _seed_login(db_session, op.id, datetime(2026, 6, 16, 7, 50))  # different day

    data = client.get("/api/v1/analytics/attendance", params=_RANGE).json()["data"]
    row = next(u for u in data["users"] if u["userId"] == op.id)
    days = {d["date"]: d for d in row["days"]}
    assert days["2026-06-15"]["firstLoginAt"].startswith("2026-06-15T08:05")
    assert days["2026-06-15"]["loginCount"] == 2
    assert days["2026-06-16"]["firstLoginAt"].startswith("2026-06-16T07:50")
    assert days["2026-06-16"]["loginCount"] == 1


def test_attendance_filters_by_role(client, db_session):
    op = _seed_user(db_session, role="operador", full_name="Op")
    seller = _seed_user(db_session, role="vendedor", full_name="Vende")
    _seed_login(db_session, op.id, datetime(2026, 6, 15, 8, 0))
    _seed_login(db_session, seller.id, datetime(2026, 6, 15, 8, 0))

    data = client.get(
        "/api/v1/analytics/attendance", params={**_RANGE, "role": "operador"}
    ).json()["data"]
    assert [u["userId"] for u in data["users"]] == [op.id]


def test_attendance_empty_range(client):
    # Range far in the past: no login falls there (not even conftest's admin).
    data = client.get(
        "/api/v1/analytics/attendance",
        params={"from": "2020-01-01", "to": "2020-01-31"},
    ).json()["data"]
    assert data["users"] == []
