"""Authentication and authorization dependencies.

``get_current_user`` resolves the authenticated user from the JWT; ``require_role``
restricts by role and ``require_permission`` by **area** (key of the
``RESOURCE_ROLES`` matrix). Endpoints declare intent with
``Depends(require_permission("orders:write"))`` and the matrix stays the single
source of truth. ``require_role`` validates against the roles **read from the DB**
(via ``get_current_user``), so a role change takes effect instantly. A user with
several roles is granted the UNION of their permissions.
"""

from typing import Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.modules.users.permissions import RESOURCE_ROLES
from src.modules.users.service import UserService, user_service
from src.shared.exceptions import AuthenticationError, AuthorizationError
from src.shared.security import decode_access_token

# auto_error=False: we handle the 401 ourselves via AuthenticationError so it
# goes through the uniform {errors, meta} wrapper instead of FastAPI's HTTPException.
bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    svc: UserService = Depends(user_service),
) -> UserModel:
    """Resolves the authenticated user from the ``Authorization: Bearer <jwt>`` header."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Falta el token de autenticación")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("Token de sesión inválido") from exc
    user = svc.get(user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Usuario no encontrado o inactivo")
    return user


def require_role(*roles: UserRole):
    """Dependency factory: requires the user to hold at least one of ``roles``.

    e.g. ``Depends(require_role(UserRole.ADMIN))``. Prefer ``require_permission``
    when an area key exists in ``RESOURCE_ROLES`` (centralizes the policy).
    """

    def dependency(current_user: UserModel = Depends(get_current_user)) -> UserModel:
        if not current_user.has_any_role(*roles):
            raise AuthorizationError("No tienes permiso para realizar esta acción")
        return current_user

    return dependency


def require_permission(resource: str):
    """Per-area authorization dependency, resolved against ``RESOURCE_ROLES``.

    ``Depends(require_permission("orders:write"))``. A nonexistent key blows up
    with ``KeyError`` at **router load time** (not at runtime), catching typos in CI.
    """
    return require_role(*RESOURCE_ROLES[resource])


def get_branch_scope(
    current_user: UserModel = Depends(get_current_user),
) -> Optional[int]:
    """Resolves the authenticated user's branch scope.

    ``None`` for the **global** roles (administrador and vendedor): they see and
    operate all branches. The assigned ``branch_id`` for the workshop roles
    (operador and canteador), bound to their own. Since ``get_current_user`` reads
    the fresh user from the DB on every request, reassigning their branch takes
    effect instantly. A workshop role with no assigned branch is an invalid state
    (403). Note: the vendedor keeps their ``branch_id`` as a **base branch**
    (default on create, see ``resolve_branch_for_create``), even though their
    reads are global. The global roles never combine with another, so a user
    holding several roles is always a workshop one.
    """
    if current_user.is_global:
        return None
    if current_user.branch_id is None:
        raise AuthorizationError(
            "Tu usuario no tiene una sucursal asignada; contacta al administrador."
        )
    return current_user.branch_id
