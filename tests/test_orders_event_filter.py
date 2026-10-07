"""The listing's event filter: ``dateField`` + ``dateFrom``/``dateTo`` + ``actorId``.

Seeded straight through ``db_session``, like ``test_analytics_performance.py``:
the filter hinges on exact instants and actors, which the state machine would
stamp with the real clock and the logged-in user.

Every instant is Ecuador's wall clock (``_at``) stored as the naive UTC the app
writes: the days are the business's, UTC-5.
"""

from datetime import datetime, timedelta

import pytest

from main import app
from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.orders.model import (
    OrderActivityModel,
    OrderModel,
    OrderStatusHistoryModel,
)
from src.modules.users.model import UserModel
from tests.order_helpers import _token_for


def _at(day, hour, minute=0):
    """June 2026 on Ecuador's wall clock (UTC-5), as the naive UTC the app stores."""
    return datetime(2026, 6, day, hour, minute) + timedelta(hours=5)


def _user(db, name, roles):
    user = UserModel(
        email=f"{name.lower()}@e.com",
        full_name=name,
        hashed_password="x",
        roles=roles,
        branch_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _move(from_status, to_status, at, actor):
    return OrderStatusHistoryModel(
        from_status=from_status,
        to_status=to_status,
        actor="staff",
        actor_user_id=actor.id,
        actor_label=actor.full_name,
        created_at=at,
    )


def _order(db, client_id, *, branch_id=1, status, created_by, history, **columns):
    order = OrderModel(
        client_id=client_id,
        branch_id=branch_id,
        status=status,
        optimization_snapshot={},
        optimization_hash="h",
        currency="USD",
        subtotal=100.0,
        total=100.0,
        total_boards_used=1,
        created_by=created_by.id,
        history=history,
        **columns,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _closed(db, order, kind, at, by):
    db.add(
        OrderActivityModel(
            order_id=order.id,
            type=kind,
            status="done",
            started_at=at - timedelta(hours=1),
            started_by=by.id,
            finished_at=at,
            finished_by=by.id,
        )
    )
    db.commit()


@pytest.fixture
def seeded(db_session):
    """Three orders and the people who moved them.

    ``a`` walks the whole way: sold by Sara, paid at Carla's desk, cut by Oscar
    (closed at 23:30 on the 3rd -- 04:30 UTC on the 4th, still the 3rd), banded
    and finished by Bruno, dispatched by Sara. On the 10th somebody marks it as
    priority: a ``finished -> finished`` row that is no event.

    ``b`` enters the shop on the 3rd, is rolled back to the queue on the 8th --
    which is no second payment -- and enters it again on the 9th.

    ``c`` is sold by Pedro and cancelled by Carla before paying.
    """
    db = db_session
    db.add(ClientModel(identifier="0100000397"))
    db.commit()
    sara = _user(db, "Sara", ["vendedor"])
    pedro = _user(db, "Pedro", ["vendedor"])
    carla = _user(db, "Carla", ["administrador"])
    oscar = _user(db, "Oscar", ["operador"])
    bruno = _user(db, "Bruno", ["canteador"])

    a = _order(
        db,
        1,
        status="dispatched",
        created_by=sara,
        created_at=_at(1, 10),
        queued_at=_at(2, 9),
        dispatched_at=_at(6, 11),
        dispatched_by=sara.id,
        history=[
            _move("confirmed", "queued", _at(2, 9), carla),
            _move("queued", "in_process", _at(3, 8), oscar),
            _move("in_process", "finished", _at(5, 16), bruno),
            _move("finished", "dispatched", _at(6, 11), sara),
            _move("dispatched", "dispatched", _at(10, 9), carla),
        ],
    )
    _closed(db, a, "cutting", _at(3, 23, 30), oscar)
    _closed(db, a, "banding", _at(5, 16), bruno)

    b = _order(
        db,
        1,
        status="in_process",
        created_by=sara,
        created_at=_at(1, 11),
        queued_at=_at(2, 10),
        history=[
            _move("confirmed", "queued", _at(2, 10), carla),
            _move("queued", "in_process", _at(3, 9), oscar),
            _move("in_process", "queued", _at(8, 9), carla),
            _move("queued", "in_process", _at(9, 9), oscar),
        ],
    )
    c = _order(
        db,
        1,
        status="cancelled",
        created_by=pedro,
        created_at=_at(1, 12),
        history=[_move("confirmed", "cancelled", _at(4, 10), carla)],
    )
    people = {"sara": sara, "pedro": pedro, "carla": carla, "oscar": oscar}
    return {"a": a.id, "b": b.id, "c": c.id, **people, "bruno": bruno}


def _ids(client, headers=None, **params):
    resp = client.get(
        "/api/v1/orders/", params={"sort": "oldest", **params}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return [o["id"] for o in resp.json()["data"]]


def _on(event, first, last=None):
    return {
        "dateField": event,
        "dateFrom": f"2026-06-{first:02d}",
        "dateTo": f"2026-06-{(last or first):02d}",
    }


def test_each_event_is_read_where_it_is_written(client, seeded):
    a, b, c = seeded["a"], seeded["b"], seeded["c"]

    assert _ids(client, **_on("created", 1)) == [a, b, c]
    assert _ids(client, **_on("paid", 2)) == [a, b]
    assert _ids(client, **_on("in_process", 3)) == [a, b]
    assert _ids(client, **_on("cut_done", 3)) == [a]
    assert _ids(client, **_on("banding_done", 5)) == [a]
    assert _ids(client, **_on("additional_done", 1, 30)) == []
    assert _ids(client, **_on("finished", 5)) == [a]
    assert _ids(client, **_on("dispatched", 6)) == [a]
    assert _ids(client, **_on("cancelled", 4)) == [c]


def test_the_days_are_the_business_days(client, seeded):
    # Closed at 23:30 on the 3rd, local: 04:30 UTC on the 4th, still the 3rd.
    assert _ids(client, **_on("cut_done", 3)) == [seeded["a"]]
    assert _ids(client, **_on("cut_done", 4)) == []
    # Open ends.
    assert _ids(client, dateField="finished", dateFrom="2026-06-05") == [seeded["a"]]
    assert _ids(client, dateField="finished", dateTo="2026-06-04") == []


def test_the_actor_is_the_one_of_that_event(client, seeded):
    a, b = seeded["a"], seeded["b"]
    who = {name: seeded[name].id for name in ("sara", "carla", "oscar", "bruno")}

    # Sara sold both, but the payment was registered at Carla's desk.
    assert _ids(client, dateField="created", actorId=who["sara"]) == [a, b]
    assert _ids(client, dateField="paid", actorId=who["carla"]) == [a, b]
    assert _ids(client, dateField="paid", actorId=who["sara"]) == []
    # Oscar closed the cut and Bruno the banding: never one for the other.
    assert _ids(client, dateField="cut_done", actorId=who["oscar"]) == [a]
    assert _ids(client, dateField="cut_done", actorId=who["bruno"]) == []
    assert _ids(client, dateField="banding_done", actorId=who["bruno"]) == [a]
    assert _ids(client, dateField="finished", actorId=who["bruno"]) == [a]
    assert _ids(client, dateField="dispatched", actorId=who["sara"]) == [a]
    # Who and when are one condition: Bruno closed nothing on the 3rd.
    assert _ids(client, **_on("cut_done", 3), actorId=who["bruno"]) == []


def test_a_same_status_row_is_no_event(client, seeded):
    # The 10th only carries the priority mark on ``a`` (dispatched -> dispatched).
    for event in ("in_process", "finished", "dispatched", "cancelled"):
        assert _ids(client, **_on(event, 10)) == [], event


def test_a_rollback_is_no_second_payment_but_entering_again_counts(client, seeded):
    assert _ids(client, **_on("paid", 8, 30)) == []
    assert _ids(client, **_on("in_process", 9)) == [seeded["b"]]


def test_the_event_alone_filters_nothing(client, seeded):
    every = [seeded["a"], seeded["b"], seeded["c"]]
    assert _ids(client, dateField="cancelled") == every
    assert _ids(client) == every


def test_the_old_created_range_still_works(client, seeded):
    assert _ids(client, createdFrom="2026-06-01", createdTo="2026-06-01") == [
        seeded["a"],
        seeded["b"],
        seeded["c"],
    ]
    # Combined with an event, it narrows it.
    assert _ids(client, createdTo="2026-05-31", **_on("paid", 2)) == []


def test_an_inverted_range_is_rejected(client, seeded):
    resp = client.get(
        "/api/v1/orders/", params={"dateFrom": "2026-06-05", "dateTo": "2026-06-04"}
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "dateFrom"


def test_the_operator_still_sees_only_their_branch(client, db_session, seeded):
    other = BranchModel(code="MACAS", name="Macas", is_active=True)
    db_session.add(other)
    db_session.commit()
    elsewhere = _order(
        db_session,
        1,
        branch_id=other.id,
        status="finished",
        created_by=seeded["sara"],
        created_at=_at(1, 9),
        history=[_move("in_process", "finished", _at(5, 10), seeded["bruno"])],
    )
    headers = _token_for(client, db_session, "operador")

    assert _ids(client, **_on("finished", 5)) == [seeded["a"], elsewhere.id]
    assert _ids(client, headers=headers, **_on("finished", 5)) == [seeded["a"]]


def test_the_listing_names_the_seller(client, seeded):
    data = client.get("/api/v1/orders/", params={"sort": "oldest"}).json()["data"]
    assert [(o["createdBy"], o["createdByName"]) for o in data] == [
        (seeded["sara"].id, "Sara"),
        (seeded["sara"].id, "Sara"),
        (seeded["pedro"].id, "Pedro"),
    ]


def test_the_contract_shows_the_event_filter_not_the_old_range():
    listing = app.openapi()["paths"]["/api/v1/orders/"]["get"]
    params = {p["name"] for p in listing["parameters"]}
    assert {"dateField", "dateFrom", "dateTo", "actorId"} <= params
    assert not params & {"createdFrom", "createdTo"}
