"""The data change of ``000000000008``, run against real rows.

Every ``additional`` row that existed before the workshop codes was created by
a BILLED service, and none of those orders has a piece with a code -- so a
``pending`` one could never start under the new floor and would hold its order
out of ``finished`` for good. The migration drops exactly those, and nothing
else: an ``in_progress`` row still closes (zero worked pieces, zero missing), a
``done`` one is the bander's history, and a closed order holds nothing back.

On an empty database (the only place a migration is normally exercised) the
statement is a no-op, which is why it lives as a module constant.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from tests.order_helpers import (
    _order_with_billed_service_only,
    _patch_activity,
    _patch_status,
    _to_finished,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000008_workshop_codes.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("_workshop_codes_mig", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_additional(db_session, order_id, status):
    """The row the old rule built from the billed service."""
    db_session.execute(
        text(
            "INSERT INTO order_activities (order_id, type, status, created_at, "
            "updated_at) VALUES (:i, 'additional', :s, now(), now())"
        ),
        {"i": order_id, "s": status},
    )
    db_session.commit()


def _additional_status(db_session, order_id):
    return db_session.execute(
        text(
            "SELECT status FROM order_activities "
            "WHERE order_id = :i AND type = 'additional'"
        ),
        {"i": order_id},
    ).scalar()


def _drop(db_session):
    db_session.execute(text(_migration().DROP_UNWORKED_ADDITIONAL))
    db_session.commit()


def test_a_pending_row_on_an_open_order_is_dropped(client, db_session):
    order = _order_with_billed_service_only(client, db_session, identifier="0100000496")
    assert _patch_status(client, order["id"], "queued").status_code == 200
    _legacy_additional(db_session, order["id"], "pending")

    _drop(db_session)
    assert _additional_status(db_session, order["id"]) is None


def test_a_started_row_is_kept_and_can_still_close(client, db_session):
    """Zero worked pieces means zero missing: the bander can still close it."""
    order = _order_with_billed_service_only(client, db_session, identifier="0100000504")
    assert _patch_status(client, order["id"], "queued").status_code == 200
    assert (
        _patch_activity(client, order["id"], "cutting", "in_progress").status_code
        == 200
    )
    _legacy_additional(db_session, order["id"], "in_progress")

    _drop(db_session)
    assert _additional_status(db_session, order["id"]) == "in_progress"
    closed = _patch_activity(client, order["id"], "additional", "done")
    assert closed.status_code == 200


def test_a_closed_orders_history_is_kept(client, db_session):
    order = _order_with_billed_service_only(client, db_session, identifier="0100000512")
    _to_finished(client, order["id"])
    _legacy_additional(db_session, order["id"], "pending")

    _drop(db_session)
    assert _additional_status(db_session, order["id"]) == "pending"


def test_the_downgrade_restores_the_billed_rule_for_open_orders(client, db_session):
    order = _order_with_billed_service_only(client, db_session, identifier="0100000520")
    assert _additional_status(db_session, order["id"]) is None

    db_session.execute(text(_migration().RESTORE_BILLED_ADDITIONAL))
    db_session.commit()
    assert _additional_status(db_session, order["id"]) == "pending"
