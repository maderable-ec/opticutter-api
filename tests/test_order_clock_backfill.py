"""The status-clock backfill of ``000000000002``, run against real rows.

The statement hinges on a condition that is silent when wrong -- it has to skip
the ``from == to`` audit rows -- and on an empty database (the only place a
migration is normally exercised) it is a no-op. So the suite builds the exact
shapes through the API, blanks the column, replays the SQL, and checks it lands
back where the service had put it.

Its sibling ``banding_ready_at`` is gone: ``000000000003`` moved that clock into
``order_activities.ready_at``, so the column the old statement writes no longer
exists. The equivalent coverage for the activity backfills lives in
``test_order_activities_backfill.py``, which is also where the JSON-null trap
(``edges IS NOT NULL`` matching every row) is now pinned.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from tests.order_helpers import _order_with_banding, _patch_status, _to_in_process

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
    db_session.execute(text(mig.BACKFILL_STATUS_CHANGED_AT))
    db_session.commit()


def _column(db_session, order_id, column):
    return db_session.execute(
        text(f"SELECT {column} FROM orders WHERE id = :i"), {"i": order_id}
    ).scalar()


def test_backfill_recovers_the_status_clock(client, db_session):
    order = _order_with_banding(client, db_session, identifier="0100000397")
    _to_in_process(client, order["id"])  # paid, then the shop starts the cut
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


def test_backfill_recovers_the_clock_of_a_never_moved_order(client, db_session):
    """An order still in `confirmed` is dated from its creation row.

    That row is the one case where ``from_status IS NULL``, which is why the
    filter keeps it instead of skipping every non-transition alike.
    """
    order = _order_with_banding(client, db_session, identifier="0100000447")
    live = _column(db_session, order["id"], "status_changed_at")

    _replay(db_session, "status_changed_at")
    assert _column(db_session, order["id"], "status_changed_at") == live
