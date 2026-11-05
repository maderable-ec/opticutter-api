"""The saved-quote backfill of ``000000000022``, run against real rows.

The notation's ``L`` is now the pair of sides along the LONGER measure, and a
quote saved before kept the sides the web picked by the old reading -- on a
wider-than-tall row, the pair the seller did NOT mean. The backfill moves them
so every quote keeps the text the seller typed. On an empty database the
migration is a no-op, so the suite saves quotes through the API, replays the
backfill and reads them back.
"""

import importlib.util
import pathlib

from src.modules.preorders.model import PreOrderModel

from .test_edge_banding import _create_edge_banding
from .test_orders import _BRANCH, _create_board, _create_client

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000022_edge_notation_by_length.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("_notation_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(label, height, width, sides, special=()):
    return {
        "priority": 0,
        "height": height,
        "width": width,
        "quantity": 1,
        "materialKey": "b1",
        "label": label,
        "canRotate": True,
        **({"edgeBanding": {"sides": sides}} if sides else {}),
        **({"specialEdges": list(special)} if special else {}),
    }


def _saved_quote(client):
    """A quote saved the way the web used to save it: ``1L`` is ``left``."""
    c = _create_client(client)
    b = _create_board(client)
    tape = _create_edge_banding(client, code="BLN", band_type="Hard", alias="BLN")
    rows = [
        _row("Tira", 70, 2050, ["left", "right"]),  # typed 2L
        _row("Lateral", 300, 600, ["left"]),  # typed 1L
        _row("Alto", 600, 300, ["left"]),  # typed 1L, already the long side
        _row("Tapa", 300, 600, ["top", "bottom", "left", "right"]),  # 4L
        _row("Fondo", 300, 600, None),
        _row("Repisa", 300, 600, ["left"], [{"side": "top", "productId": tape["id"]}]),
    ]
    resp = client.post(
        "/api/v1/preorders/",
        json={
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": rows,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _stored(db_session, preorder_id):
    db_session.expire_all()
    rows = db_session.get(PreOrderModel, preorder_id).requirements
    return {
        r["label"]: (
            (r.get("edge_banding") or {}).get("sides"),
            [e["side"] for e in r.get("special_edges") or []],
        )
        for r in rows
    }


def _notations(client, preorder_id):
    """The text the quote's plan prints for each piece."""
    data = client.get(f"/api/v1/preorders/{preorder_id}").json()["data"]
    return {
        p["pieceId"]: (p.get("edges") or {}).get("notation")
        for layout in data["optimization"]["layouts"]
        for p in layout["placedPieces"]
    }


def _replay(db_session):
    written = _migration().backfill(db_session.connection())
    db_session.commit()
    return written


def test_backfill_keeps_the_text_the_seller_typed(client, db_session):
    pre = _saved_quote(client)
    # Read by length, the saved sides say the opposite on the wider rows.
    assert _notations(client, pre["id"])["Lateral"] == "1C"

    assert _replay(db_session) == 1
    assert _stored(db_session, pre["id"]) == {
        "Tira": (["top", "bottom"], []),
        "Lateral": (["top"], []),
        "Alto": (["left"], []),  # its height is the longer measure: untouched
        "Tapa": (["top", "bottom", "left", "right"], []),  # the same sides
        "Fondo": (None, []),
        "Repisa": (["top"], ["left"]),
    }
    printed = _notations(client, pre["id"])
    assert printed["Tira"] == "2L"
    assert printed["Lateral"] == "1L"
    assert printed["Alto"] == "1L"
    assert printed["Tapa"] == "4L"
    # The Canto on the long side, the special tape on a short one -- as typed.
    assert printed["Repisa"] == "1L · 1C CD BLN"


def test_backfill_bills_the_side_the_text_meant(client, db_session):
    pre = _saved_quote(client)
    _replay(db_session)
    data = client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]
    # 2×2050 (Tira) + 600 (Lateral) + 600 (Alto) + 2×300+2×600 (Tapa)
    # + 600 (Repisa's Canto) + 300 (its special tape), in metres.
    assert data["optimization"]["totalEdgeBandingLinearM"] == 8.0


def test_backfill_leaves_a_quote_with_no_wider_banded_row_alone(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    resp = client.post(
        "/api/v1/preorders/",
        json={
            "clientId": c["id"],
            "branchId": _BRANCH,
            "materials": [{"key": "b1", "source": "catalog", "productId": b["id"]}],
            "requirements": [_row("Alto", 600, 300, ["left"])],
        },
    )
    pre = resp.json()["data"]
    assert _replay(db_session) == 0
    assert _stored(db_session, pre["id"]) == {"Alto": (["left"], [])}


def test_the_downgrade_puts_the_sides_back(client, db_session):
    pre = _saved_quote(client)
    before = _stored(db_session, pre["id"])
    _replay(db_session)
    _replay(db_session)  # the downgrade runs the same swap
    assert _stored(db_session, pre["id"]) == before
