"""Integration: the stock question a seller asks while quoting.

Runs against real rows — products with their ``external_code``, branches with
their warehouse — because that join is the thing worth testing here. The
vendor's MySQL is replaced by an injected ``fetch``, the same device the catalog
sync uses, so nothing in this file needs a live SIFAC.
"""

import pytest

from src.modules.branches.model import BranchModel
from src.modules.inventory.external_inventory import StockRow
from src.modules.inventory.service import StockService
from src.modules.products.model import ProductModel, ProductType
from src.modules.users.model import UserModel
from src.shared.exceptions import ExternalServiceError
from src.shared.security import create_access_token

_URL = "/api/v1/inventory/stock-check"

# Sucúa holds plenty of the white board and almost no black tape; Macas holds
# nothing at all, which is what the live catalog actually looks like.
_STOCK = [
    StockRow(external_code="TABLEROS:155", warehouse_code=1, quantity=558.0),
    StockRow(external_code="TABLEROS:155", warehouse_code=2, quantity=0.0),
    StockRow(external_code="TABLEROS:340", warehouse_code=1, quantity=2.0),
    StockRow(external_code="TAPACANTOS:278", warehouse_code=1, quantity=12.5),
]


@pytest.fixture
def stocked(db_session):
    """Two branches with warehouses, plus a small catalog wired to the codes."""
    sucua = BranchModel(code="SUCUA", name="Sucúa", is_active=True, warehouse_code=1)
    macas = BranchModel(code="MACAS", name="Macas", is_active=True, warehouse_code=2)
    nowhere = BranchModel(code="OTRA", name="Sin bodega", is_active=True)
    db_session.add_all([sucua, macas, nowhere])

    board = ProductModel(
        type=ProductType.BOARD.value,
        code="155",
        external_code="TABLEROS:155",
        name="MDP RH BLANCO NIEVE",
        price=20.0,
        is_active=True,
        attributes={"subtype": "MDP"},
    )
    scarce = ProductModel(
        type=ProductType.BOARD.value,
        code="340",
        external_code="TABLEROS:340",
        name="MDP RH ANTRACITA",
        price=25.0,
        is_active=True,
        attributes={"subtype": "Plywood"},
    )
    tape = ProductModel(
        type=ProductType.EDGE_BANDING.value,
        code="278",
        external_code="TAPACANTOS:278",
        name="TAPACANTO BLANCO 19X0.45",
        price=0.5,
        is_active=True,
        attributes={},
    )
    handmade = ProductModel(
        type=ProductType.BOARD.value,
        code="MANUAL-1",
        external_code=None,
        name="Tablero cargado a mano",
        price=10.0,
        is_active=True,
        attributes={},
    )
    db_session.add_all([board, scarce, tape, handmade])
    db_session.commit()
    return {
        "sucua": sucua,
        "macas": macas,
        "nowhere": nowhere,
        "board": board,
        "scarce": scarce,
        "tape": tape,
        "handmade": handmade,
    }


def _service(db_session, rows=None, broken=False):
    def fetch():
        if broken:
            raise ExternalServiceError("SIFAC caída")
        return list(_STOCK if rows is None else rows)

    return StockService(db_session, fetch=fetch)


def _check(svc, branch, items):
    from src.modules.inventory.schemas import StockCheckItem

    return svc.check(
        branch.id, [StockCheckItem(product_id=p.id, quantity=q) for p, q in items]
    )


# --------------------------------------------------------------------------- #
# The two reasons
# --------------------------------------------------------------------------- #


def test_a_well_stocked_board_raises_nothing(db_session, stocked):
    svc = _service(db_session)
    result = _check(svc, stocked["sucua"], [(stocked["board"], 4)])
    assert result.checked is True
    assert result.alerts == []


def test_below_the_threshold_is_reported(db_session, stocked):
    # 2 sheets against the default floor of 5.
    svc = _service(db_session)
    (alert,) = _check(svc, stocked["sucua"], [(stocked["scarce"], 1)]).alerts
    assert alert.below_threshold is True
    assert alert.insufficient is False
    assert alert.available == 2.0
    assert alert.threshold == 5.0
    assert alert.unit == "sheets"
    assert alert.product_name == "MDP RH ANTRACITA"


def test_not_enough_for_the_quote_is_reported_even_above_the_threshold(
    db_session, stocked
):
    """The quote needs 600 sheets and the branch holds 558: over the floor and
    still impossible, which is precisely the case a threshold cannot catch."""
    svc = _service(db_session)
    (alert,) = _check(svc, stocked["sucua"], [(stocked["board"], 600)]).alerts
    assert alert.below_threshold is False
    assert alert.insufficient is True
    assert alert.required == 600.0


def test_both_reasons_ride_together(db_session, stocked):
    svc = _service(db_session)
    (alert,) = _check(svc, stocked["sucua"], [(stocked["scarce"], 9)]).alerts
    assert (alert.below_threshold, alert.insufficient) == (True, True)


def test_edge_banding_is_measured_in_metres(db_session, stocked):
    """12.5 m against the 50 m floor: low, though 12.5 sheets never would be."""
    svc = _service(db_session)
    (alert,) = _check(svc, stocked["sucua"], [(stocked["tape"], 3)]).alerts
    assert alert.unit == "linear_m"
    assert alert.threshold == 50.0
    assert alert.below_threshold is True


def test_the_same_product_twice_is_added_up(db_session, stocked):
    """Two blocks of one board (say, one squared and one not) are two payload
    materials and two order lines, but one physical pile in the warehouse."""
    svc = _service(db_session)
    (alert,) = _check(
        svc, stocked["sucua"], [(stocked["board"], 300), (stocked["board"], 300)]
    ).alerts
    assert alert.required == 600.0
    assert alert.insufficient is True


def test_alerts_come_worst_first(db_session, stocked):
    svc = _service(db_session)
    result = _check(
        svc,
        stocked["sucua"],
        [(stocked["scarce"], 1), (stocked["board"], 600)],
    )
    # What the branch cannot cover at all leads, then the lowest.
    assert [a.insufficient for a in result.alerts] == [True, False]


# --------------------------------------------------------------------------- #
# What cannot be answered
# --------------------------------------------------------------------------- #


def test_a_branch_without_a_warehouse_answers_unchecked(db_session, stocked):
    svc = _service(db_session)
    result = _check(svc, stocked["nowhere"], [(stocked["scarce"], 1)])
    assert result.checked is False
    assert result.alerts == []
    assert result.branch.code == "OTRA"


def test_an_unreachable_vendor_answers_unchecked_not_an_error(db_session, stocked):
    """A quote must never depend on a third party's database being up."""
    svc = _service(db_session, broken=True)
    result = _check(svc, stocked["sucua"], [(stocked["scarce"], 1)])
    assert result.checked is False
    assert result.alerts == []


def test_a_hand_made_product_is_unknown_not_zero(db_session, stocked):
    """No ``external_code`` means the vendor has no opinion; reporting zero
    would park a permanent false alarm on something SIFAC cannot restock."""
    svc = _service(db_session)
    assert _check(svc, stocked["sucua"], [(stocked["handmade"], 10)]).alerts == []


def test_an_article_macas_never_stocked_reads_as_zero(db_session, stocked):
    svc = _service(db_session)
    (alert,) = _check(svc, stocked["macas"], [(stocked["board"], 1)]).alerts
    assert alert.available == 0.0
    assert (alert.below_threshold, alert.insufficient) == (True, True)


def test_no_items_is_a_checked_empty_answer(db_session, stocked):
    svc = _service(db_session)
    result = svc.check(stocked["sucua"].id, [])
    assert result.checked is True and result.alerts == []


def test_an_unknown_branch_is_a_404(db_session, stocked):
    from src.shared.exceptions import EntityNotFoundError

    svc = _service(db_session)
    with pytest.raises(EntityNotFoundError):
        svc.check(9999, [])


# --------------------------------------------------------------------------- #
# HTTP surface and permissions
# --------------------------------------------------------------------------- #


def _as(anon_client, db_session, role, branch=None):
    user = UserModel(
        email=f"{role}@test.com",
        hashed_password="x",
        role=role,
        full_name=role,
        is_active=True,
        branch_id=branch.id if branch else None,
    )
    db_session.add(user)
    db_session.commit()
    anon_client.headers.update(
        {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}
    )
    return anon_client


def test_endpoint_answers_the_seller(client, db_session, stocked, monkeypatch):
    monkeypatch.setattr(
        "src.modules.inventory.service.fetch_stock", lambda: list(_STOCK)
    )
    response = client.post(
        _URL,
        json={
            "branchId": stocked["sucua"].id,
            "items": [{"productId": stocked["scarce"].id, "quantity": 1}],
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["checked"] is True
    assert data["branch"]["code"] == "SUCUA"
    (alert,) = data["alerts"]
    assert alert["belowThreshold"] is True
    assert alert["productName"] == "MDP RH ANTRACITA"
    assert alert["unit"] == "sheets"


def test_the_shop_floor_cannot_ask(anon_client, db_session, stocked):
    """``operador``/``canteador`` do not quote, and stock is a commercial datum."""
    for role in ("operador", "canteador"):
        c = _as(anon_client, db_session, role, branch=stocked["sucua"])
        response = c.post(_URL, json={"branchId": stocked["sucua"].id, "items": []})
        assert response.status_code == 403


def test_the_stock_map_is_read_once_and_survives_the_cache(db_session, stocked):
    """Cached in Redis because this runs on every quote, pre-order read and order
    detail, against a third party's production database.

    The round trip is the part worth pinning: JSON object keys are strings, so
    the warehouse code comes back as ``"1"`` and has to be turned back into an
    int — a product would otherwise read as zero everywhere on the second call.
    """
    reads = {"n": 0}

    def fetch():
        reads["n"] += 1
        return list(_STOCK)

    svc = StockService(db_session, fetch=fetch)
    first = _check(svc, stocked["sucua"], [(stocked["scarce"], 1)])
    # A second service instance, to prove the cache and not some instance state.
    second = _check(
        StockService(db_session, fetch=fetch),
        stocked["sucua"],
        [(stocked["scarce"], 1)],
    )

    assert reads["n"] == 1
    assert [a.available for a in first.alerts] == [a.available for a in second.alerts]
    assert second.alerts[0].available == 2.0


# --------------------------------------------------------------------------- #
# The low-stock report (admin only, under /analytics)
# --------------------------------------------------------------------------- #
_REPORT_URL = "/api/v1/analytics/low-stock"


def test_report_lists_everything_under_its_threshold(db_session, stocked):
    svc = _service(db_session)
    report = svc.low_stock()
    assert report.checked is True
    assert report.thresholds.board == 5.0
    assert report.thresholds.edge_banding == 50.0
    found = {(i.code, i.branch.code) for i in report.items}
    # Sucúa: the scarce board (2) and the tape (12.5). Macas: everything at zero.
    assert ("340", "SUCUA") in found
    assert ("278", "SUCUA") in found
    assert ("155", "SUCUA") not in found  # 558 sheets
    assert ("155", "MACAS") in found


def test_report_counts_a_zero_stocked_article(db_session, stocked):
    """Not an omission: a product at zero is the most urgent row on the page."""
    svc = _service(db_session)
    macas = [i for i in svc.low_stock(branch_id=stocked["macas"].id).items]
    assert macas and all(i.available == 0.0 for i in macas)


def test_report_carries_the_material_subtype(db_session, stocked):
    """Read off the product's ``attributes``, so the report can be narrowed the
    way the catalog is (MDP vs Plywood) without a second request per row."""
    svc = _service(db_session)
    by_code = {i.code: i for i in svc.low_stock(branch_id=stocked["sucua"].id).items}
    assert by_code["340"].subtype == "Plywood"
    # None, not an error, for a product whose attributes never carried one.
    assert by_code["278"].subtype is None


def test_report_filters_by_branch_and_by_type(db_session, stocked):
    svc = _service(db_session)
    boards = svc.low_stock(
        branch_id=stocked["sucua"].id, product_type=ProductType.BOARD.value
    )
    assert {i.code for i in boards.items} == {"340"}
    tapes = svc.low_stock(
        branch_id=stocked["sucua"].id, product_type=ProductType.EDGE_BANDING.value
    )
    assert {i.code for i in tapes.items} == {"278"}


def test_report_skips_a_branch_with_no_warehouse(db_session, stocked):
    """The vendor's system simply has no inventory for it; a page of zeros
    would read as an emergency instead of as a missing configuration."""
    svc = _service(db_session)
    assert svc.low_stock(branch_id=stocked["nowhere"].id).items == []


def test_report_ignores_products_the_vendor_never_synced(db_session, stocked):
    svc = _service(db_session)
    codes = {i.code for i in svc.low_stock().items}
    assert "MANUAL-1" not in codes


def test_report_orders_by_branch_then_type_then_emptiest(db_session, stocked):
    svc = _service(db_session)
    items = svc.low_stock().items
    keys = [(i.branch.id, i.type, i.available) for i in items]
    assert keys == sorted(keys)


def test_report_says_unchecked_when_the_vendor_is_down(db_session, stocked):
    svc = _service(db_session, broken=True)
    report = svc.low_stock()
    assert report.checked is False
    assert report.items == []
    # The thresholds still come back: they are ours, not the vendor's.
    assert report.thresholds.board == 5.0


def test_report_endpoint_is_admin_only(anon_client, db_session, stocked):
    for role in ("vendedor", "operador", "canteador"):
        c = _as(anon_client, db_session, role, branch=stocked["sucua"])
        assert c.get(_REPORT_URL).status_code == 403


def test_report_endpoint_serves_the_admin(client, db_session, stocked, monkeypatch):
    monkeypatch.setattr(
        "src.modules.inventory.service.fetch_stock", lambda: list(_STOCK)
    )
    response = client.get(_REPORT_URL, params={"branchId": stocked["sucua"].id})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["checked"] is True
    assert data["thresholds"] == {"board": 5.0, "edgeBanding": 50.0}
    assert {i["code"] for i in data["items"]} == {"340", "278"}
    assert data["items"][0]["branch"]["name"] == "Sucúa"
