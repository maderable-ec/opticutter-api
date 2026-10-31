"""Unit: a user holds a list of roles (no DB).

The permissions are the union of the roles, and only the workshop roles combine:
a bander learning to cut is ``operador`` + ``canteador``. The order of the list
is canonical, and its first element is the primary role the legacy single-role
fields still report.
"""

import pytest

from src.modules.users.dependencies import get_branch_scope
from src.modules.users.enums import UserRole, canonical_roles
from src.modules.users.model import UserModel
from src.modules.users.schemas import UserUpdate
from src.modules.users.service import UserService
from src.shared.exceptions import ValidationError


def _user(*roles: str, branch_id=1) -> UserModel:
    return UserModel(
        id=1,
        email="x@y.com",
        hashed_password="x",
        roles=list(roles),
        branch_id=branch_id,
    )


def test_canonical_roles_orders_and_deduplicates():
    assert canonical_roles(["canteador", "OPERADOR", "canteador"]) == [
        "operador",
        "canteador",
    ]
    assert canonical_roles([UserRole.BANDER, UserRole.ADMIN]) == [
        "administrador",
        "canteador",
    ]
    assert canonical_roles([]) == []


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        canonical_roles(["superusuario"])


@pytest.mark.parametrize(
    "roles",
    [
        ["operador"],
        ["canteador"],
        ["vendedor"],
        ["administrador"],
        ["proyectista"],
        ["canteador", "operador"],
    ],
)
def test_allowed_role_sets(mock_session, roles):
    assert UserService(mock_session)._normalize_roles(roles) == canonical_roles(roles)


@pytest.mark.parametrize(
    "roles",
    [
        ["administrador", "vendedor"],
        ["administrador", "operador"],
        ["vendedor", "operador"],
        ["vendedor", "canteador"],
        ["proyectista", "vendedor"],
        ["proyectista", "operador"],
    ],
)
def test_global_roles_never_combine(mock_session, roles):
    with pytest.raises(ValidationError) as exc:
        UserService(mock_session)._normalize_roles(roles)
    assert exc.value.field == "roles"


def test_empty_role_list_is_rejected(mock_session):
    with pytest.raises(ValidationError) as exc:
        UserService(mock_session)._normalize_roles([])
    assert exc.value.field == "roles"


def test_the_apprentice_is_scoped_to_their_branch():
    """Two workshop roles are still a workshop user: bound to one branch."""
    assert (
        get_branch_scope(current_user=_user("operador", "canteador", branch_id=7)) == 7
    )


@pytest.mark.parametrize("role", ["administrador", "vendedor"])
def test_global_roles_see_every_branch(role):
    assert get_branch_scope(current_user=_user(role)) is None


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"is_active": False, "password": "nueva-clave-1"}, "isActive"),
        ({"roles": ["vendedor"], "full_name": "Otra"}, "roles"),
    ],
)
def test_an_admin_cannot_lock_themselves_out(mock_session, changes, field):
    """Refused before any field is assigned: the session holds the user untouched."""
    admin = _user("administrador", branch_id=None)
    mock_session.get.return_value = admin
    with pytest.raises(ValidationError) as exc:
        UserService(mock_session).update(1, UserUpdate(**changes), actor_id=1)
    assert exc.value.field == field
    assert admin.is_active is not False
    assert admin.hashed_password == "x"
    assert admin.roles == ["administrador"]
    assert admin.full_name is None
    mock_session.commit.assert_not_called()


def test_the_designer_is_last_in_the_canonical_order():
    """Added at the end, so no stored list of the existing roles reorders."""
    assert list(UserRole)[-1] is UserRole.DESIGNER
    assert canonical_roles(["proyectista"]) == ["proyectista"]


def test_the_designer_needs_a_base_branch(mock_session):
    with pytest.raises(ValidationError) as exc:
        UserService(mock_session)._normalize_branch(["proyectista"], None)
    assert exc.value.field == "branchId"
