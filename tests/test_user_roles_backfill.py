"""The two role migrations' SQL, run against real rows.

``000000000013`` turned ``users.role`` into ``users.roles``; ``000000000016``
drops ``role`` and, on the way back, restores it from ``roles[1]``. On an empty
database (the only place a migration is normally exercised) both statements are
no-ops, so the suite seeds users of every role through the service and replays
them. The schema the tests build has no ``role`` any more, so the test brings it
back and loosens ``roles`` first -- inside one transaction that is rolled back
at the end: Postgres DDL is transactional, and the schema the rest of the suite
shares is never touched.
"""

import importlib.util
import pathlib

from sqlalchemy import text

from src.modules.users.enums import UserRole
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_migration_{name}", _VERSIONS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(db_session) -> None:
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
    svc.create(
        UserCreate(
            email="aprendiz@empresa.com",
            password="supersecret",
            roles=[UserRole.BANDER, UserRole.OPERATOR],
            branch_id=1,
        )
    )


def test_downgrade_restores_the_primary_role_and_013_rebuilds_the_list(db_session):
    _seed(db_session)
    try:
        db_session.execute(text("ALTER TABLE users ADD COLUMN role varchar(32)"))
        db_session.execute(
            text(_migration("000000000016_drop_users_role").RESTORE_ROLE)
        )
        restored = db_session.execute(
            text("SELECT role, roles FROM users ORDER BY id")
        ).all()

        db_session.execute(
            text(
                "ALTER TABLE users DROP CONSTRAINT ck_users_roles_not_empty, "
                "ALTER COLUMN roles DROP NOT NULL"
            )
        )
        db_session.execute(text("UPDATE users SET roles = NULL"))
        db_session.execute(text(_migration("000000000013_user_roles").BACKFILL_ROLES))
        rebuilt = db_session.execute(
            text("SELECT role, roles FROM users ORDER BY id")
        ).all()
    finally:
        db_session.rollback()

    # The mirror is the primary role: the first of the canonical list.
    assert [role for role, _ in restored] == [r.value for r in UserRole] + ["operador"]
    assert all(roles[0] == role for role, roles in restored)
    # 013 turns each single role back into a one-element list.
    assert all(roles == [role] for role, roles in rebuilt)
