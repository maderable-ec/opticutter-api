"""The cut-list material backfill of ``000000000005``, run against real rows.

The statement pairs ``order_pieces`` with the snapshot's ``requirements`` by
POSITION, which is the only thing there is to pair on -- and which is silent
when it is wrong: off by one, it writes the neighbour's material rather than
none. On an empty database (the only place a migration is normally exercised)
it is a no-op, so the suite builds the shape through the service, blanks the
columns, replays the SQL and checks it lands back where ``create()`` put it.

The order used here carries TWO materials, one of them a client's offcut: that
is the case the column exists for, since an offcut resolves to no catalog
product and ``product_id`` is NULL for every piece cut from it.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from tests.order_helpers import _order_on_board_and_offcut

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000005_order_piece_material.py"
)

_COLUMNS = ("material_key", "product_code", "product_name")


def _migration():
    spec = importlib.util.spec_from_file_location(
        "_piece_material_migration", _MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pieces(db_session, order_id):
    rows = db_session.execute(
        text(
            "SELECT id, material_key, product_code, product_name, product_id "
            "FROM order_pieces WHERE order_id = :i ORDER BY id"
        ),
        {"i": order_id},
    ).all()
    return [dict(r._mapping) for r in rows]


def _blank(db_session, order_id):
    db_session.execute(
        text(
            f"UPDATE order_pieces SET {', '.join(f'{c} = NULL' for c in _COLUMNS)} "
            "WHERE order_id = :i"
        ),
        {"i": order_id},
    )
    db_session.commit()


def _replay(db_session):
    db_session.execute(text(_migration().BACKFILL_PIECE_MATERIAL))
    db_session.commit()


def test_backfill_recovers_each_pieces_material(client, db_session):
    order = _order_on_board_and_offcut(client, db_session, identifier="0100000397")
    live = _pieces(db_session, order["id"])
    # The shape the test is about: two materials, and the offcut's piece has no
    # product to be named by.
    assert {p["material_key"] for p in live} == {"b1", "r1"}
    assert [p["product_id"] for p in live] == [live[0]["product_id"], None]
    assert live[0]["product_id"] is not None

    _blank(db_session, order["id"])
    _replay(db_session)

    assert _pieces(db_session, order["id"]) == live


def test_backfill_skips_an_order_whose_lists_disagree(client, db_session):
    """A piece count that does not match the snapshot means the lists are not one list.

    Positional pairing would then write a SHIFTED material onto every remaining
    piece, which reads as real data. The guard drops the whole order instead and
    leaves NULL, which the detail renders as an em dash.
    """
    order = _order_on_board_and_offcut(client, db_session, identifier="0100000405")
    first = _pieces(db_session, order["id"])[0]["id"]

    _blank(db_session, order["id"])
    db_session.execute(text("DELETE FROM order_pieces WHERE id = :i"), {"i": first})
    db_session.commit()

    _replay(db_session)

    assert [p["material_key"] for p in _pieces(db_session, order["id"])] == [None]
