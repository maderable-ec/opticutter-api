"""The roles backfill of ``000000000013``, run against real rows.

On an empty database (the only place a migration is normally exercised) the
statement is a no-op, so the suite seeds users of every role through the
service, empties ``roles`` and replays the SQL. ``roles`` is NOT NULL and
CHECKed, so the test loosens both first -- inside one transaction that is
rolled back at the end: Postgres DDL is transactional, and the schema the rest
of the suite shares is never touched.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from src.modules.users.enums import UserRole
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "000000000013_user_roles.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("_user_roles_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_turns_each_role_into_a_one_element_list(db_session):
    svc = UserService(db_session)
    for role in UserRole:
        svc.create(
            UserCreate(
                email=f"{role.value}@empresa.com",
                password="supersecret",
                roles=[role],
                branch_id=None if role is UserRole.ADMIN else 1,
            )
        )

    try:
        db_session.execute(
            text(
                "ALTER TABLE users DROP CONSTRAINT ck_users_roles_not_empty, "
                "ALTER COLUMN roles DROP NOT NULL"
            )
        )
        db_session.execute(text("UPDATE users SET roles = NULL"))
        db_session.execute(text(_migration().BACKFILL_ROLES))
        rows = db_session.execute(
            text("SELECT role, roles FROM users ORDER BY id")
        ).all()
    finally:
        db_session.rollback()

    assert [role for role, _ in rows] == [role.value for role in UserRole]
    assert all(roles == [role] for role, roles in rows)
