"""The shop's consumables: registering a blade change or a glue load, and what
each one gave.

The gate's arithmetic is pinned without a database in
``tests/unit/test_consumable_rules.py``; here it is who may register what, the
gates over HTTP, taking a change back, and the metres read off the production.
The endpoints read the real clock, so the past is seeded relative to it and the
schedule is opened around the clock: working hours are then just hours.
"""

from datetime import datetime, timedelta

from src.modules.branches.model import BranchModel
from src.modules.clients.model import ClientModel
from src.modules.consumables.model import ConsumableChangeModel
from src.modules.orders.model import (
    OrderActivityModel,
    OrderBoardModel,
    OrderModel,
    OrderPlacedPieceModel,
)
from src.modules.orders.production_service import ProductionService
from src.modules.users.model import UserModel
from tests.order_helpers import _token_for

_BASE = "/api/v1/consumables"
_REPORT = "/api/v1/analytics/consumables"
_ALL_DAY = [{"start": "00:00", "end": "24:00"}]
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _open_around_the_clock(client):
    resp = client.patch(
        "/api/v1/settings/work-schedule", json={day: _ALL_DAY for day in _DAYS}
    )
    assert resp.status_code == 200, resp.text


def _register(client, type="saw_blade", **body):
    headers = body.pop("headers", None)
    if type != "glue":
        body.setdefault("condition", "new")
    return client.post(f"{_BASE}/changes", json={"type": type, **body}, headers=headers)


def _void(client, change_id, reason=None, headers=None):
    body = {"reason": reason} if reason is not None else None
    return client.post(f"{_BASE}/changes/{change_id}/void", json=body, headers=headers)


def _status(client, headers=None, **params):
    resp = client.get(f"{_BASE}/status", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _item(branch, type):
    return next(item for item in branch["items"] if item["type"] == type)


def _error(resp):
    return resp.json()["errors"][0]


def _past(db, type="saw_blade", hours=5.0, branch_id=1, **fields):
    """A change registered ``hours`` ago, written straight to the table."""
    at = datetime.utcnow() - timedelta(hours=hours)
    if type != "glue":
        fields.setdefault("condition", "new")
    change = ConsumableChangeModel(
        branch_id=branch_id, type=type, changed_at=at, created_at=at, **fields
    )
    db.add(change)
    db.commit()
    return change


def _order(db, branch_id=1, banded_m=None, code="ORD-000001"):
    client = db.query(ClientModel).first()
    if client is None:
        client = ClientModel(identifier="0100000397")
        db.add(client)
        db.commit()
    snapshot = {} if banded_m is None else {"total_edge_banding_linear_m": banded_m}
    order = OrderModel(
        client_id=client.id,
        branch_id=branch_id,
        status="in_process",
        optimization_snapshot=snapshot,
        optimization_hash=code,
        currency="USD",
        subtotal=10.0,
        total=10.0,
        total_boards_used=1,
        code=code,
    )
    db.add(order)
    db.commit()
    return order


def _sheet(db, order, hours_ago, meters, number=1, half=False, cut=True):
    """A sheet of ``meters`` of saw travel whose one piece was marked ``hours_ago``."""
    board = OrderBoardModel(
        order_id=order.id,
        sheet_number=number,
        material_key="m",
        width=2440,
        height=1220,
        thickness=18,
        half_board=half,
        cuts=[{"length": meters * 1000}],
    )
    db.add(board)
    db.commit()
    db.add(
        OrderPlacedPieceModel(
            order_id=order.id,
            board_id=board.id,
            piece_id=f"p#{number}",
            label="p",
            x=0,
            y=0,
            width=100,
            height=100,
            original_width=100,
            original_height=100,
            rotated=False,
            cut_at=datetime.utcnow() - timedelta(hours=hours_ago) if cut else None,
        )
    )
    db.commit()


def _banding_closed(db, order, hours_ago):
    at = datetime.utcnow() - timedelta(hours=hours_ago)
    db.add(
        OrderActivityModel(
            order_id=order.id,
            type="banding",
            status="done",
            started_at=at - timedelta(minutes=30),
            finished_at=at,
        )
    )
    db.commit()


# ----------------------------------------------------------------- the status
def test_each_role_sees_the_consumables_it_registers(client, db_session):
    operator = _token_for(client, db_session, "operador")
    bander = _token_for(client, db_session, "canteador")
    seller = _token_for(client, db_session, "vendedor")

    [saw] = _status(client, headers=operator)
    assert saw["branchName"] == "Casa Matriz"
    assert [item["type"] for item in saw["items"]] == ["saw_blade", "scoring_blade"]
    # Nothing registered yet: nothing to count from, and the first one is free.
    blade = saw["items"][0]
    assert blade["lastChange"] is None and blade["meters"] is None
    assert blade["gate"] == "free" and blade["canUndo"] is False

    [edge] = _status(client, headers=bander)
    [glue] = edge["items"]
    assert (glue["type"], glue["standardQuantity"]) == ("glue", 5.0)

    [admin] = _status(client)
    assert len(admin["items"]) == 3
    assert client.get(f"{_BASE}/status", headers=seller).status_code == 403


def test_the_shop_floor_sees_its_branch_and_the_admin_all_the_active_ones(
    client, db_session
):
    macas = BranchModel(code="MACAS", name="Macas", is_active=True)
    closed = BranchModel(code="OLD", name="Cerrada", is_active=False)
    db_session.add_all([macas, closed])
    db_session.commit()
    operator = _token_for(client, db_session, "operador", branch_id=macas.id)

    assert [b["branchId"] for b in _status(client)] == [1, macas.id]
    assert [b["branchId"] for b in _status(client, branchId=macas.id)] == [macas.id]
    # Whatever they ask for, the shop floor reads its own.
    assert [b["branchId"] for b in _status(client, headers=operator, branchId=1)] == [
        macas.id
    ]


# -------------------------------------------------------------- registering
def test_an_operator_registers_a_blade_with_one_request(client, db_session):
    operator = _token_for(client, db_session, "operador")

    resp = _register(client, condition="sharpened", headers=operator)

    assert resp.status_code == 201, resp.text
    saw = _item(resp.json()["data"], "saw_blade")
    assert saw["lastChange"]["condition"] == "sharpened"
    assert saw["lastChange"]["changedByLabel"] == "Operador"
    assert saw["lastChange"]["quantity"] is None
    # Just registered: a second tap is held back, and it is theirs to undo.
    assert (saw["gate"], saw["canUndo"]) == ("blocked", True)
    assert (saw["meters"], saw["boards"]) == (0.0, 0.0)
    # The scoring blade is another consumable: untouched.
    assert _item(resp.json()["data"], "scoring_blade")["lastChange"] is None


def test_a_blade_says_whether_it_is_new_or_sharpened(client):
    resp = client.post(f"{_BASE}/changes", json={"type": "saw_blade", "branchId": 1})

    assert resp.status_code == 422
    assert _error(resp)["field"] == "condition"


def test_a_second_change_within_the_minute_is_rejected(client, db_session):
    assert _register(client, branchId=1).status_code == 201

    again = _register(client, branchId=1)

    assert again.status_code == 409
    assert "hace un momento" in _error(again)["message"]
    # The other blade of the same saw is not held back by it.
    assert _register(client, "scoring_blade", branchId=1).status_code == 201


def test_an_early_change_has_to_say_why(client, db_session):
    _open_around_the_clock(client)
    _past(db_session, hours=2)

    silent = _register(client, branchId=1)
    assert silent.status_code == 422
    assert _error(silent)["field"] == "reason"
    assert _item(_status(client)[0], "saw_blade")["gate"] == "early"

    said = _register(client, branchId=1, reason="damaged", note="Diente roto")
    assert said.status_code == 201, said.text
    last = _item(said.json()["data"], "saw_blade")["lastChange"]
    assert (last["reason"], last["note"]) == ("damaged", "Diente roto")


def test_a_reason_belongs_to_its_consumable(client, db_session):
    _open_around_the_clock(client)
    _past(db_session, hours=2)

    resp = _register(client, branchId=1, reason="pot_cleaning")

    assert resp.status_code == 422
    assert _error(resp)["field"] == "reason"


def test_past_four_hours_of_work_a_change_asks_nothing(client, db_session):
    _open_around_the_clock(client)
    _past(db_session, hours=5)
    assert _item(_status(client)[0], "saw_blade")["gate"] == "free"

    assert _register(client, branchId=1).status_code == 201


def test_the_operator_changes_blades_and_the_bander_loads_glue(client, db_session):
    operator = _token_for(client, db_session, "operador")
    bander = _token_for(client, db_session, "canteador")
    seller = _token_for(client, db_session, "vendedor")

    assert _register(client, "glue", headers=operator).status_code == 403
    assert _register(client, "saw_blade", headers=bander).status_code == 403
    assert _register(client, "saw_blade", headers=seller).status_code == 403
    assert _register(client, "glue", headers=bander).status_code == 201
    # Somebody holding both roles registers both.
    both = _token_for(client, db_session, ["operador", "canteador"])
    assert _register(client, "scoring_blade", headers=both).status_code == 201
    assert len(_status(client, headers=both)[0]["items"]) == 3


def test_a_glue_load_takes_the_standard_quantity_unless_it_says_another(
    client, db_session
):
    bander = _token_for(client, db_session, "canteador")

    standard = _register(client, "glue", headers=bander)
    assert _item(standard.json()["data"], "glue")["lastChange"]["quantity"] == 5.0
    first_id = _item(standard.json()["data"], "glue")["lastChange"]["id"]

    # The office retunes the standard: the control starts there from now on...
    resp = client.patch("/api/v1/settings/consumables", json={"glueLoadKg": 4})
    assert resp.json()["data"]["glueLoadKg"] == 4.0
    assert _item(_status(client, headers=bander)[0], "glue")["standardQuantity"] == 4.0
    # ...and the load already registered keeps the kg it took.
    assert db_session.get(ConsumableChangeModel, first_id).quantity == 5.0

    _open_around_the_clock(client)
    db_session.query(ConsumableChangeModel).update(
        {"changed_at": datetime.utcnow() - timedelta(hours=6)}
    )
    db_session.commit()
    odd = _register(client, "glue", quantity=3.5, headers=bander)
    assert _item(odd.json()["data"], "glue")["lastChange"]["quantity"] == 3.5
    assert _register(client, "glue", quantity=0, headers=bander).status_code == 422


def test_the_standard_glue_load_is_the_admin_s_to_set(client, db_session):
    assert client.get("/api/v1/settings/consumables").json()["data"] == {
        "glueLoadKg": 5.0
    }
    bander = _token_for(client, db_session, "canteador")
    assert client.get("/api/v1/settings/consumables", headers=bander).status_code == 403
    resp = client.patch("/api/v1/settings/consumables", json={"glueLoadKg": 0})
    assert resp.status_code == 422


def test_a_change_goes_to_the_registrant_s_branch(client, db_session):
    macas = BranchModel(code="MACAS", name="Macas", is_active=True)
    db_session.add(macas)
    db_session.commit()
    operator = _token_for(client, db_session, "operador")

    # The shop floor never picks a branch, and cannot pick somebody else's.
    assert _register(client, branchId=macas.id, headers=operator).status_code == 403
    own = _register(client, headers=operator)
    assert own.json()["data"]["branchId"] == 1

    # The admin has none of their own: they have to say which.
    missing = _register(client)
    assert (missing.status_code, _error(missing)["field"]) == (422, "branchId")
    assert _register(client, branchId=999).status_code == 404
    there = _register(client, branchId=macas.id)
    assert there.json()["data"]["branchName"] == "Macas"
    # One saw per branch: Macas's change does not block Matriz's, nor the reverse.
    assert _item(there.json()["data"], "saw_blade")["gate"] == "blocked"


# ------------------------------------------------------------ taking it back
def test_whoever_registered_a_change_undoes_it_with_one_tap(client, db_session):
    _open_around_the_clock(client)
    earlier = _past(db_session, hours=30, condition="sharpened")
    operator = _token_for(client, db_session, "operador")
    done = _register(client, headers=operator)
    change_id = _item(done.json()["data"], "saw_blade")["lastChange"]["id"]

    resp = _void(client, change_id, headers=operator)

    assert resp.status_code == 200, resp.text
    saw = _item(resp.json()["data"], "saw_blade")
    # The blade that was on is on again, with the gate it had.
    assert saw["lastChange"]["id"] == earlier.id
    assert (saw["gate"], saw["canUndo"]) == ("free", False)
    row = db_session.get(ConsumableChangeModel, change_id)
    assert row.voided_at is not None and row.voided_by_label == "Operador"
    # Idempotent, and the slot is free again at once.
    assert _void(client, change_id, headers=operator).status_code == 200
    assert _register(client, headers=operator).status_code == 201


def test_past_the_window_or_for_somebody_else_s_change_it_is_the_admin_s(
    client, db_session
):
    operator = _token_for(client, db_session, "operador")
    other = _token_for(client, db_session, "operador", email="otro@empresa.com")
    bander = _token_for(client, db_session, "canteador")
    done = _register(client, headers=operator)
    change_id = _item(done.json()["data"], "saw_blade")["lastChange"]["id"]

    assert _void(client, change_id, headers=other).status_code == 403
    assert _void(client, change_id, headers=bander).status_code == 403

    row = db_session.get(ConsumableChangeModel, change_id)
    row.created_at = row.changed_at = datetime.utcnow() - timedelta(minutes=20)
    db_session.commit()
    late = _void(client, change_id, headers=operator)
    assert late.status_code == 403
    assert "administrador" in _error(late)["message"]
    assert _item(_status(client, headers=operator)[0], "saw_blade")["canUndo"] is False

    # The admin voids any, and says why.
    silent = _void(client, change_id)
    assert (silent.status_code, _error(silent)["field"]) == (422, "reason")
    resp = _void(client, change_id, reason="Se registró por error")
    assert resp.status_code == 200
    db_session.refresh(row)
    assert row.void_reason == "Se registró por error"
    assert _item(resp.json()["data"], "saw_blade")["lastChange"] is None


def test_a_change_of_another_branch_does_not_exist_for_the_shop_floor(
    client, db_session
):
    macas = BranchModel(code="MACAS", name="Macas", is_active=True)
    db_session.add(macas)
    db_session.commit()
    there = _past(db_session, branch_id=macas.id)
    operator = _token_for(client, db_session, "operador")

    assert _void(client, there.id, headers=operator).status_code == 404
    assert _void(client, 999, reason="x").status_code == 404


# --------------------------------------------------------- a change in the past
def test_the_admin_enters_a_change_somebody_forgot(client, db_session):
    _open_around_the_clock(client)
    _past(db_session, hours=2)
    operator = _token_for(client, db_session, "operador")
    yesterday = (datetime.utcnow() - timedelta(hours=26)).isoformat() + "Z"
    tomorrow = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"

    assert _register(client, changedAt=yesterday, headers=operator).status_code == 403
    future = _register(client, branchId=1, changedAt=tomorrow)
    assert (future.status_code, _error(future)["field"]) == (422, "changedAt")

    # A correction, not a tap on the shop floor: no reason asked, however close
    # it lands to the one registered two hours ago.
    resp = _register(client, branchId=1, changedAt=yesterday, condition="sharpened")
    assert resp.status_code == 201, resp.text
    # The one on the saw is still the latest by when it happened.
    assert _item(resp.json()["data"], "saw_blade")["lastChange"]["condition"] == "new"
    assert db_session.query(ConsumableChangeModel).count() == 2
    # It cannot land on top of another one.
    assert _register(client, branchId=1, changedAt=yesterday).status_code == 409


# ------------------------------------------------------------------ the metres
def test_a_blade_counts_the_sheets_closed_since_it_went_on(client, db_session):
    _open_around_the_clock(client)
    order = _order(db_session)
    _sheet(db_session, order, hours_ago=30, meters=14.0, number=1)  # the blade before
    _past(db_session, "saw_blade", hours=24)
    _sheet(db_session, order, hours_ago=20, meters=15.5, number=2)
    _past(db_session, "scoring_blade", hours=10)
    _sheet(db_session, order, hours_ago=5, meters=12.0, number=3, half=True)
    _sheet(db_session, order, hours_ago=1, meters=9.0, number=4, cut=False)  # unmarked

    branch = _status(client)[0]

    saw, scoring = _item(branch, "saw_blade"), _item(branch, "scoring_blade")
    assert (saw["meters"], saw["boards"]) == (27.5, 1.5)
    assert (scoring["meters"], scoring["boards"]) == (12.0, 0.5)
    assert saw["orders"] is None
    assert 23.9 < saw["workedHours"] < 24.1


def test_the_glue_counts_the_bandings_closed_since_it_was_loaded(client, db_session):
    _open_around_the_clock(client)
    before = _order(db_session, banded_m=80.0, code="ORD-000001")
    _banding_closed(db_session, before, hours_ago=30)
    _past(db_session, "glue", hours=24, quantity=5.0)
    for index, meters in enumerate((120.5, 60.0)):
        order = _order(db_session, banded_m=meters, code=f"ORD-00001{index}")
        _banding_closed(db_session, order, hours_ago=10 - index)

    glue = _item(_status(client)[0], "glue")

    assert (glue["meters"], glue["orders"], glue["boards"]) == (180.5, 2, None)


# ------------------------------------------------------------------ the report
def _seed_lives(db):
    """Three saw lives (one broke, one in use), their sheets, and two glue loads."""
    order = _order(db)
    _past(db, hours=100, condition="new")
    _sheet(db, order, hours_ago=90, meters=600.0, number=1)
    _sheet(db, order, hours_ago=80, meters=400.0, number=2)
    _past(db, hours=70, condition="sharpened")
    _sheet(db, order, hours_ago=69, meters=50.0, number=3)
    # Came off after an hour and a half: it broke.
    _past(db, hours=68.5, condition="sharpened", reason="damaged", note="Diente roto")
    _sheet(db, order, hours_ago=60, meters=700.0, number=4)
    _past(db, hours=40, condition="new")
    _sheet(db, order, hours_ago=30, meters=20.0, number=5)

    _past(db, "glue", hours=100, quantity=5.0)
    banded = _order(db, banded_m=900.0, code="ORD-000002")
    _banding_closed(db, banded, hours_ago=80)
    _past(db, "glue", hours=50, quantity=4.0)


def test_the_report_reads_consumption_and_yield_off_the_lives(client, db_session):
    _open_around_the_clock(client)
    _seed_lives(db_session)

    resp = client.get(_REPORT)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    [branch] = data["branches"]
    saw = _item(branch, "saw_blade")
    # Four blades went on: two new, two sharpened.
    assert (saw["changes"], saw["newCount"], saw["sharpenedCount"]) == (4, 2, 2)
    # Three came off; the one that broke is counted apart and kept out of the
    # averages, so the yield is 1000 m (new) and 700 m (sharpened).
    assert (saw["closedLives"], saw["damagedLives"]) == (3, 1)
    assert saw["avgMeters"] == 850.0
    assert (saw["new"]["lives"], saw["new"]["avgMeters"]) == (1, 1000.0)
    assert (saw["sharpened"]["lives"], saw["sharpened"]["avgMeters"]) == (1, 700.0)
    assert saw["avgBoards"] == 1.5
    assert (saw["current"]["meters"], saw["current"]["inUse"]) == (20.0, True)

    glue = _item(branch, "glue")
    assert (glue["changes"], glue["quantity"]) == (2, 9.0)
    assert (glue["closedLives"], glue["metersPerKg"]) == (1, 180.0)
    assert glue["newCount"] is None and glue["avgBoards"] is None
    # Nobody registered a scoring blade: every figure is empty, not missing.
    scoring = _item(branch, "scoring_blade")
    assert (scoring["changes"], scoring["current"]) == (0, None)

    # The lives behind it: the ones in use first, then newest closed first.
    saws = [life for life in data["lives"] if life["type"] == "saw_blade"]
    assert [life["meters"] for life in saws] == [20.0, 700.0, 50.0, 1000.0]
    broken = saws[2]
    assert (broken["endReason"], broken["endNote"]) == ("damaged", "Diente roto")
    assert broken["condition"] == "sharpened" and broken["inUse"] is False
    assert data["lives"][0]["inUse"] and data["lives"][1]["inUse"]


def test_the_lives_add_up_to_the_saw_s_own_metres(client, db_session):
    """The same sheets as Producción counts: no metre lost or counted twice."""
    _open_around_the_clock(client)
    _seed_lives(db_session)
    first = datetime.utcnow() - timedelta(hours=100)

    lives = client.get(_REPORT).json()["data"]["lives"]

    boards = ProductionService(db_session).processed_boards(
        first, datetime.utcnow() + timedelta(seconds=1)
    )
    saw_meters = sum(life["meters"] for life in lives if life["type"] == "saw_blade")
    assert saw_meters == sum(board.cut_linear_m for board in boards) == 1770.0


def test_a_life_is_placed_in_the_range_by_the_day_the_blade_came_off(
    client, db_session
):
    _open_around_the_clock(client)
    _past(db_session, hours=24 * 40, condition="new")
    _past(db_session, hours=24 * 35, condition="sharpened")
    _past(db_session, hours=1, condition="new")
    day = (datetime.utcnow() - timedelta(hours=5, days=35)).date()

    params = {"from": str(day - timedelta(days=1)), "to": str(day + timedelta(days=1))}
    data = client.get(_REPORT, params=params).json()["data"]

    saw = _item(data["branches"][0], "saw_blade")
    # The blade mounted 40 days ago came off inside the range; the one mounted
    # in it is still counted as a unit that went in.
    assert (saw["changes"], saw["closedLives"]) == (1, 1)
    closed = [life for life in data["lives"] if not life["inUse"]]
    assert [life["condition"] for life in closed] == ["new"]
    # Today's blade comes along whatever the range.
    assert saw["current"]["inUse"] is True


def test_a_voided_change_counts_for_nothing(client, db_session):
    _open_around_the_clock(client)
    _past(db_session, hours=50)
    mistake = _past(db_session, hours=20)
    order = _order(db_session)
    _sheet(db_session, order, hours_ago=10, meters=30.0)

    assert _void(client, mistake.id, reason="Nadie cambió la sierra").status_code == 200

    data = client.get(_REPORT).json()["data"]
    saw = _item(data["branches"][0], "saw_blade")
    assert (saw["changes"], saw["closedLives"]) == (1, 0)
    assert saw["current"]["meters"] == 30.0


def test_the_report_is_the_admin_s_and_narrows_to_a_branch(client, db_session):
    macas = BranchModel(code="MACAS", name="Macas", is_active=True)
    db_session.add(macas)
    db_session.commit()
    _past(db_session, branch_id=macas.id)
    operator = _token_for(client, db_session, "operador")

    assert client.get(_REPORT, headers=operator).status_code == 403
    both = client.get(_REPORT).json()["data"]
    assert [b["branchName"] for b in both["branches"]] == ["Casa Matriz", "Macas"]
    one = client.get(_REPORT, params={"branchId": macas.id}).json()["data"]
    assert [b["branchId"] for b in one["branches"]] == [macas.id]
    assert [life["branchName"] for life in one["lives"]] == ["Macas"]


def test_a_user_deleted_since_keeps_their_changes(client, db_session):
    operator = _token_for(client, db_session, "operador")
    assert _register(client, headers=operator).status_code == 201
    user = db_session.query(UserModel).filter_by(email="operador@empresa.com").one()
    db_session.delete(user)
    db_session.commit()

    saw = _item(_status(client)[0], "saw_blade")

    assert saw["lastChange"]["changedByLabel"] == "Operador"
