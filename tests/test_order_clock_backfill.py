"""The migration's backfills, run against real rows.

``000000000002`` adds ``status_changed_at``/``banding_ready_at`` and dates the
orders that already exist. Both statements hinge on a condition that is silent
when wrong -- one skips the ``from == to`` audit rows, the other tells JSON
``null`` from SQL NULL -- and on an empty database (the only place a migration is
normally exercised) both are no-ops. So the suite builds the exact shapes through
the API, blanks the columns, replays the SQL, and checks it lands back where the
service had put it.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from tests.test_order_banding import (
    _banded_pieces,
    _cut_piece,
    _order_mixed_pieces,
    _order_with_banding,
    _patch_status,
    _to_cutting,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000002_status_and_banding_clocks.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("_clock_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(db_session, *columns):
    """Blanks the columns the service wrote, then replays the migration's SQL."""
    mig = _migration()
    db_session.execute(
        text(f"UPDATE orders SET {', '.join(f'{c} = NULL' for c in columns)}")
    )
    if "status_changed_at" in columns:
        db_session.execute(text(mig.BACKFILL_STATUS_CHANGED_AT))
    if "banding_ready_at" in columns:
        db_session.execute(text(mig.BACKFILL_BANDING_READY_AT))
    db_session.commit()


def _column(db_session, order_id, column):
    return db_session.execute(
        text(f"SELECT {column} FROM orders WHERE id = :i"), {"i": order_id}
    ).scalar()


def test_backfill_recovers_the_status_clock(client, db_session):
    order = _order_with_banding(client, db_session, identifier="0100000397")
    _to_cutting(client, order["id"])  # queued, then cutting
    live = _column(db_session, order["id"], "status_changed_at")

    _replay(db_session, "status_changed_at")
    assert _column(db_session, order["id"], "status_changed_at") == live


def test_backfill_ignores_the_priority_audit_row(client, db_session):
    """The `from == to` row is later than the real transition, and must not win.

    Without the filter this order would come back dated at the moment it was
    flagged urgent -- i.e. the flag itself would reset the clock, and the board
    would stop showing the very order somebody escalated.
    """
    order = _order_with_banding(client, db_session, identifier="0100000405")
    assert _patch_status(client, order["id"], "queued").status_code == 200
    real = _column(db_session, order["id"], "status_changed_at")

    resp = client.patch(
        f"/api/v1/orders/{order['id']}/priority", json={"isPriority": True}
    )
    assert resp.status_code == 200

    _replay(db_session, "status_changed_at")
    assert _column(db_session, order["id"], "status_changed_at") == real


def test_backfill_falls_back_to_created_at_without_history(client, db_session):
    order = _order_with_banding(client, db_session, identifier="0100000413")
    db_session.execute(
        text("DELETE FROM order_status_history WHERE order_id = :i"),
        {"i": order["id"]},
    )
    db_session.commit()

    _replay(db_session, "status_changed_at")
    assert _column(db_session, order["id"], "status_changed_at") == _column(
        db_session, order["id"], "created_at"
    )


def test_backfill_dates_the_banding_clock_from_the_first_banded_piece(
    client, db_session
):
    order = _order_mixed_pieces(client, db_session, identifier="0100000421")
    _to_cutting(client, order["id"])
    banded = _banded_pieces(client, order["id"])
    assert _cut_piece(client, order["id"], banded[0]).status_code == 200
    assert _cut_piece(client, order["id"], banded[1]).status_code == 200
    live = _column(db_session, order["id"], "banding_ready_at")
    assert live is not None

    _replay(db_session, "banding_ready_at")
    assert _column(db_session, order["id"], "banding_ready_at") == live


def test_backfill_does_not_date_the_clock_off_a_plain_piece(client, db_session):
    """The JSON-null trap: `edges IS NOT NULL` would match every row here.

    This order has no edge banding at all, so however many pieces get cut the
    banding clock must stay NULL.
    """
    order = _order_mixed_pieces(client, db_session, identifier="0100000439")
    _to_cutting(client, order["id"])
    plan = client.get(f"/api/v1/orders/{order['id']}/cutting-plan").json()["data"]
    plain = [p for b in plan["boards"] for p in b["pieces"] if not p["edges"]]
    assert plain, "fixture must carry pieces without banding"
    assert _cut_piece(client, order["id"], plain[0]).status_code == 200

    _replay(db_session, "banding_ready_at")
    assert _column(db_session, order["id"], "banding_ready_at") is None


def test_backfill_recovers_the_clock_of_a_never_moved_order(client, db_session):
    """An order still in `confirmed` is dated from its creation row.

    That row is the one case where ``from_status IS NULL``, which is why the
    filter keeps it instead of skipping every non-transition alike.
    """
    order = _order_with_banding(client, db_session, identifier="0100000447")
    live = _column(db_session, order["id"], "status_changed_at")

    _replay(db_session, "status_changed_at")
    assert _column(db_session, order["id"], "status_changed_at") == live
