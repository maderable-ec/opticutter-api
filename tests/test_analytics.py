"""Tests for the analytics reports outside ``performance.py``: the date range,
bottlenecks and attendance.

Seeding goes straight through ``db_session`` to pin statuses, ``created_at``, and history
precisely (the state machine doesn't let every case be reached cleanly).
"""

from datetime import datetime

from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    ActivityType,
    OrderActivityModel,
    OrderModel,
    OrderStatusHistoryModel,
)
from src.modules.users.login_event_model import UserLoginEventModel
from src.modules.users.model import UserModel

_BASE = datetime(2026, 6, 15, 12, 0, 0)
_RANGE = {"from": "2026-06-01", "to": "2026-06-30"}


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
    """``role`` is one role or a list of them."""
    user = UserModel(
        email=email or f"{full_name.replace(' ', '').lower()}@e.com",
        full_name=full_name,
        hashed_password="x",
        roles=[role] if isinstance(role, str) else list(role),
        branch_id=branch_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_login(db, user_id, created_at):
    db.add(UserLoginEventModel(user_id=user_id, created_at=created_at))
    db.commit()


# ------------------------------------------------------------------ date range
def test_default_range_when_omitted(client):
    data = client.get("/api/v1/analytics/branch-comparison").json()["data"]
    assert data["range"]["dateFrom"] < data["range"]["dateTo"]


def test_invalid_range_returns_422_with_envelope(client):
    resp = client.get(
        "/api/v1/analytics/branch-comparison",
        params={"from": "2026-06-30", "to": "2026-06-01"},
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "from"


def test_date_filter_is_half_open_on_the_business_days(client, db_session):
    """``from``/``to`` are Ecuador's days (UTC-5): each starts at 05:00 UTC.

    A UTC day would run 19:00 to 19:00 on the shop floor, so the edges are
    seeded one minute either side of the LOCAL midnights.
    """
    _seed_clients(db_session)
    # 00:00 on June 1st and 23:59 on June 10th, local: both inside.
    _seed_order(db_session, total=100.0, created_at=datetime(2026, 6, 1, 5, 0, 0))
    _seed_order(db_session, total=50.0, created_at=datetime(2026, 6, 11, 4, 59, 0))
    # 23:59 on May 31st and 00:00 on June 11th, local: both outside.
    _seed_order(db_session, total=999.0, created_at=datetime(2026, 6, 1, 4, 59, 0))
    _seed_order(db_session, total=999.0, created_at=datetime(2026, 6, 11, 5, 0, 0))

    data = client.get(
        "/api/v1/analytics/branch-comparison",
        params={"from": "2026-06-01", "to": "2026-06-10"},
    ).json()["data"]
    assert data["total"]["orders"]["entered"] == 2


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
        # 07:00 to 10:00 on the shop floor (UTC-5): the bucket is the LOCAL day.
        _hist("cutting", datetime(2026, 6, 2, 12, 0)),
        _hist("cut", datetime(2026, 6, 2, 15, 0), from_status="cutting"),
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


def test_role_filter_matches_a_user_holding_that_role(client, db_session):
    """A bander learning to cut shows up under both roles, once each."""
    apprentice = _seed_user(
        db_session, role=["canteador", "operador"], full_name="Aprendiz"
    )
    bander = _seed_user(db_session, role="canteador", full_name="Canta")
    op = _seed_user(db_session, role="operador", full_name="Op")
    for user in (apprentice, bander, op):
        _seed_login(db_session, user.id, datetime(2026, 6, 15, 8, 0))

    def _ids(role):
        data = client.get(
            "/api/v1/analytics/attendance", params={**_RANGE, "role": role}
        ).json()["data"]
        return sorted(u["userId"] for u in data["users"])

    assert _ids("canteador") == sorted([apprentice.id, bander.id])
    assert _ids("operador") == sorted([apprentice.id, op.id])

    rows = client.get("/api/v1/analytics/attendance", params=_RANGE).json()["data"]
    row = next(u for u in rows["users"] if u["userId"] == apprentice.id)
    assert row["roles"] == ["operador", "canteador"]


def test_attendance_empty_range(client):
    # Range far in the past: no login falls there (not even conftest's admin).
    data = client.get(
        "/api/v1/analytics/attendance",
        params={"from": "2020-01-01", "to": "2020-01-31"},
    ).json()["data"]
    assert data["users"] == []


def test_bottlenecks_skip_an_activity_registered_after_the_work(client, db_session):
    """Started and closed seconds apart: the tap, not the job, so no sample."""
    _seed_clients(db_session)
    order = _seed_order(db_session, status="in_process")
    db_session.add(
        _seed_activity(
            order.id,
            ActivityType.banding,
            started_at=datetime(2026, 6, 15, 10, 0, 0),
            finished_at=datetime(2026, 6, 15, 10, 0, 8),
        )
    )
    other = _seed_order(db_session, status="in_process", optimization_hash="h2")
    db_session.add(
        _seed_activity(
            other.id,
            ActivityType.banding,
            started_at=datetime(2026, 6, 15, 10, 0),
            finished_at=datetime(2026, 6, 15, 12, 0),
        )
    )
    db_session.commit()

    data = client.get("/api/v1/analytics/bottlenecks", params=_RANGE).json()["data"]
    banding = next(s for s in data["stages"] if s["key"] == "banding")
    assert banding["sampleCount"] == 1
    assert banding["medianHours"] == 2.0
