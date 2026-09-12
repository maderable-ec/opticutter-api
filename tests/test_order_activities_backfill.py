"""The activity backfills of ``000000000003``, run against real rows.

The migration builds one ``order_activities`` row per applicable activity out of
the OLD vocabulary -- the cut's clocks from the ``cutting``/``cut`` history rows,
the banding from the eight columns it is replacing, the additional work from the
snapshot's service lines. On an empty database (the only place a migration is
normally exercised) all three are no-ops, and two of them hinge on a condition
that is silent when wrong: the cut takes ``MIN`` and not ``MAX`` of its history
rows (an admin rollback would otherwise re-date the operator's start), and the
additional work reads a JSON array whose key may not exist at all.

So the suite builds the shapes through the API, deletes the rows the service
wrote, replays the SQL, and checks it lands back on the same values.

The banding's own trap -- ``edges IS NOT NULL`` is TRUE for every row of a
``JSON`` column, because SQLAlchemy persists Python ``None`` as the JSON value
``null`` -- is pinned from the service side in ``test_order_activities.py``; the
migration no longer reads ``edges`` at all, since it copies a column.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from tests.order_helpers import (
    _cut_all_pieces,
    _order_with_banding,
    _order_with_services,
    _order_without_banding,
    _patch_activity,
    _patch_status,
    _to_in_process,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000003_order_activities.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("_activities_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(db_session, order_id, *, status=None, banding=None):
    """Deletes the rows the service wrote and replays the migration's inserts.

    ``status``/``banding`` restore the pre-migration vocabulary the SQL reads:
    the statements run BEFORE ``orders.status`` is rewritten and while the eight
    banding columns still exist, neither of which is true any more. Faking them
    is what lets the real statement be exercised.
    """
    mig = _migration()
    db_session.execute(
        text("DELETE FROM order_activities WHERE order_id = :i"), {"i": order_id}
    )
    if status is not None:
        db_session.execute(
            text("UPDATE orders SET status = :s WHERE id = :i"),
            {"s": status, "i": order_id},
        )
    if banding is not None:
        db_session.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS banding_status VARCHAR(16)"
            )
        )
        for column in (
            "banding_ready_at",
            "banding_started_at",
            "banding_finished_at",
        ):
            db_session.execute(
                text(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} TIMESTAMP")
            )
        for column in ("banding_started_by", "banding_finished_by"):
            db_session.execute(
                text(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} INTEGER")
            )
        for column in ("banding_started_by_label", "banding_finished_by_label"):
            db_session.execute(
                text(
                    f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} VARCHAR(128)"
                )
            )
        db_session.execute(
            text("UPDATE orders SET banding_status = :s WHERE id = :i"),
            {"s": banding, "i": order_id},
        )
        db_session.execute(
            text(
                "UPDATE orders SET banding_status = 'not_applicable' WHERE banding_status IS NULL"
            )
        )
    db_session.execute(text(mig.BACKFILL_CUTTING_ACTIVITY))
    if banding is not None:
        db_session.execute(text(mig.BACKFILL_BANDING_ACTIVITY))
    db_session.execute(text(mig.BACKFILL_ADDITIONAL_ACTIVITY))
    db_session.commit()


def _rows(db_session, order_id):
    return {
        row.type: row
        for row in db_session.execute(
            text(
                "SELECT type, status, ready_at, started_at, finished_at "
                "FROM order_activities WHERE order_id = :i"
            ),
            {"i": order_id},
        ).all()
    }


def _drop_legacy_banding_columns(db_session):
    """Undoes the columns ``_replay`` re-added, so the next test sees the real schema."""
    for column in (
        "banding_status",
        "banding_ready_at",
        "banding_started_at",
        "banding_started_by",
        "banding_started_by_label",
        "banding_finished_at",
        "banding_finished_by",
        "banding_finished_by_label",
    ):
        db_session.execute(text(f"ALTER TABLE orders DROP COLUMN IF EXISTS {column}"))
    db_session.commit()


def test_backfill_gives_every_order_a_cut_activity(client, db_session):
    """The cut is on every order, whatever state it was left in."""
    order = _order_without_banding(client, db_session, identifier="0100000397")
    assert _patch_status(client, order["id"], "queued").status_code == 200

    _replay(db_session, order["id"], status="queued")
    rows = _rows(db_session, order["id"])
    assert set(rows) == {"cutting"}
    assert rows["cutting"].status == "pending"
    # Reaching the queue is when the cut stopped being blocked.
    assert rows["cutting"].ready_at is not None


def test_backfill_dates_the_cut_from_its_history_rows(client, db_session):
    """``cutting``/``cut`` were a pair of statuses: that is where the clocks are."""
    order = _order_without_banding(client, db_session, identifier="0100000405")
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert _patch_activity(client, order["id"], "cutting", "done").status_code == 200
    live = _rows(db_session, order["id"])["cutting"]

    # The history says `in_process` today, so write the old pair the SQL reads.
    db_session.execute(
        text(
            "UPDATE order_status_history SET to_status = 'cutting', from_status = 'queued' "
            "WHERE order_id = :i AND to_status = 'in_process'"
        ),
        {"i": order["id"]},
    )
    db_session.execute(
        text(
            "UPDATE order_status_history SET to_status = 'cut', from_status = 'cutting' "
            "WHERE order_id = :i AND to_status = 'finished'"
        ),
        {"i": order["id"]},
    )
    db_session.commit()

    _replay(db_session, order["id"], status="cut")
    row = _rows(db_session, order["id"])["cutting"]
    assert row.status == "done"
    assert row.started_at == live.started_at
    assert row.finished_at == live.finished_at


def test_backfill_ignores_a_rollback_when_dating_the_start(client, db_session):
    """``MIN``, not ``MAX``: the honest start is the first time the shop took it.

    With ``MAX`` an admin rollback (taken by mistake, sent back, taken again)
    would erase the hours the operator had already spent on it.
    """
    order = _order_without_banding(client, db_session, identifier="0100000413")
    _to_in_process(client, order["id"])
    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert (
        _patch_activity(client, order["id"], "cutting", "in_progress").status_code
        == 200
    )
    first, second = db_session.execute(
        text(
            "SELECT created_at FROM order_status_history "
            "WHERE order_id = :i AND to_status = 'in_process' ORDER BY id"
        ),
        {"i": order["id"]},
    ).scalars()
    assert first < second

    db_session.execute(
        text(
            "UPDATE order_status_history SET to_status = 'cutting' "
            "WHERE order_id = :i AND to_status = 'in_process'"
        ),
        {"i": order["id"]},
    )
    db_session.commit()

    _replay(db_session, order["id"], status="cutting")
    assert _rows(db_session, order["id"])["cutting"].started_at == first


def test_backfill_copies_the_banding_columns(client, db_session):
    """The banding's eight columns become one row -- verbatim."""
    order = _order_with_banding(client, db_session, identifier="0100000421")
    _to_in_process(client, order["id"])
    _cut_all_pieces(client, order["id"])
    assert (
        _patch_activity(client, order["id"], "banding", "in_progress").status_code
        == 200
    )
    live = _rows(db_session, order["id"])["banding"]
    assert live.ready_at is not None and live.started_at is not None

    try:
        db_session.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS banding_status VARCHAR(16)"
            )
        )
        _replay(db_session, order["id"], status="cutting", banding="in_progress")
        db_session.execute(
            text(
                "UPDATE orders SET banding_ready_at = :r, banding_started_at = :s WHERE id = :i"
            ),
            {"r": live.ready_at, "s": live.started_at, "i": order["id"]},
        )
        db_session.execute(
            text(
                "DELETE FROM order_activities WHERE order_id = :i AND type = 'banding'"
            ),
            {"i": order["id"]},
        )
        db_session.execute(text(_migration().BACKFILL_BANDING_ACTIVITY))
        db_session.commit()

        row = _rows(db_session, order["id"])["banding"]
        assert row.status == "in_progress"
        assert row.ready_at == live.ready_at
        assert row.started_at == live.started_at
    finally:
        _drop_legacy_banding_columns(db_session)


def test_backfill_skips_the_banding_of_an_order_without_canto(client, db_session):
    """``not_applicable`` meant "no work": the new shape says it with NO ROW."""
    order = _order_without_banding(client, db_session, identifier="0100000439")
    try:
        _replay(db_session, order["id"], status="confirmed", banding="not_applicable")
        assert "banding" not in _rows(db_session, order["id"])
    finally:
        _drop_legacy_banding_columns(db_session)


def test_backfill_reads_the_services_out_of_the_snapshot(client, db_session):
    """The additional work is new, so its only evidence is the frozen snapshot.

    An OPEN order gets ``pending`` (the bander has to register it, which is the
    new behavior taking effect); nothing to recover, because nothing recorded it.
    """
    order = _order_with_services(client, db_session, identifier="0100000447")
    _replay(db_session, order["id"], status="confirmed")
    rows = _rows(db_session, order["id"])
    assert rows["additional"].status == "pending"
    assert rows["additional"].started_at is None


def test_backfill_closes_the_services_of_an_already_closed_order(client, db_session):
    """A finished order must not come back looking unfinished forever."""
    order = _order_with_services(client, db_session, identifier="0100000454")
    _replay(db_session, order["id"], status="completed")
    assert _rows(db_session, order["id"])["additional"].status == "done"


def test_backfill_gives_no_additional_row_without_services(client, db_session):
    """The COALESCE case: a snapshot written before the key existed reads as zero."""
    order = _order_without_banding(client, db_session, identifier="0100000462")
    db_session.execute(
        text(
            "UPDATE orders SET optimization_snapshot = "
            "(optimization_snapshot::jsonb - 'additional_services')::json WHERE id = :i"
        ),
        {"i": order["id"]},
    )
    db_session.commit()

    _replay(db_session, order["id"], status="confirmed")
    assert "additional" not in _rows(db_session, order["id"])
