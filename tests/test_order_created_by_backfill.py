"""The order-owner backfill of ``000000000015``, run against real rows.

On an empty database (the only place a migration is normally exercised) the
statement is a no-op, so the suite mints orders through the real review flow,
blanks the column, replays the SQL and checks where it lands.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from src.modules.users.model import UserModel

from .test_preorder_review import _generate_link, _setup_preorder

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000015_order_created_by_from_quote.py"
)


def _backfill():
    spec = importlib.util.spec_from_file_location("_owner_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL_ORDER_CREATED_BY


def _confirmed_order(client):
    pre = _setup_preorder(client)
    link = _generate_link(client, pre["id"])
    client.post(f"/api/v1/public/review/{link['token']}/confirm")
    return client.get(f"/api/v1/preorders/{pre['id']}").json()["data"]


def _owner(db_session, order_id):
    return db_session.execute(
        text("SELECT created_by FROM orders WHERE id = :i"), {"i": order_id}
    ).scalar()


def _replay(db_session):
    db_session.execute(text(_backfill()))
    db_session.commit()


def test_backfill_gives_each_order_its_quotes_owner(client, db_session):
    pre = _confirmed_order(client)
    owner = _owner(db_session, pre["orderId"])
    db_session.execute(text("UPDATE orders SET created_by = NULL"))
    db_session.commit()

    _replay(db_session)
    assert owner is not None
    assert _owner(db_session, pre["orderId"]) == owner


def test_backfill_takes_the_quote_that_minted_the_order(client, db_session):
    """Two quotes may point at one order (the dedupe); the OLDEST minted it."""
    pre = _confirmed_order(client)
    first_owner = _owner(db_session, pre["orderId"])
    other = UserModel(
        email="otro@e.com", full_name="Otro", hashed_password="x", roles=["vendedor"]
    )
    db_session.add(other)
    db_session.commit()
    # A second, newer quote of the same job, pointed at the same order.
    later = client.post(f"/api/v1/preorders/{pre['id']}/duplicate").json()["data"]
    db_session.execute(
        text("UPDATE preorders SET order_id = :o, created_by = :u WHERE id = :p"),
        {"o": pre["orderId"], "u": other.id, "p": later["id"]},
    )
    db_session.execute(text("UPDATE orders SET created_by = NULL"))
    db_session.commit()

    _replay(db_session)
    assert _owner(db_session, pre["orderId"]) == first_owner


def test_backfill_never_overwrites_an_owner(client, db_session):
    pre = _confirmed_order(client)
    other = UserModel(
        email="otro@e.com", full_name="Otro", hashed_password="x", roles=["vendedor"]
    )
    db_session.add(other)
    db_session.commit()
    db_session.execute(
        text("UPDATE orders SET created_by = :u WHERE id = :o"),
        {"u": other.id, "o": pre["orderId"]},
    )
    db_session.commit()

    _replay(db_session)
    assert _owner(db_session, pre["orderId"]) == other.id
