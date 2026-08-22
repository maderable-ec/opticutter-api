from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.modules.users.schemas import ProfileUpdate, UserCreate, UserUpdate
from src.shared.crud import CRUDService, ListSort
from src.shared.database import get_db
from src.shared.exceptions import (
    AuthenticationError,
    EntityNotFoundError,
    ValidationError,
)
from src.shared.security import hash_password, verify_password


class UserService(CRUDService[UserModel, UserCreate, UserUpdate]):
    """User CRUD + authentication. Hashes the password on create/update."""

    model = UserModel
    conflict_messages = {"email": "El email ya está registrado"}

    def _normalize_branch(
        self, role_value: str, branch_id: Optional[int]
    ) -> Optional[int]:
        """Reconciles role and branch: global admin (None); staff requires a valid one.

        The administrator sees and operates all branches, so its ``branch_id`` is
        forced to ``None``. Seller/operator/bander must have an existing branch.
        """
        if role_value == UserRole.ADMIN.value:
            return None
        if branch_id is None:
            raise ValidationError(
                "El vendedor, operador o canteador requiere una sucursal "
                "asignada (branchId).",
                field="branchId",
            )
        if self.db.get(BranchModel, branch_id) is None:
            raise EntityNotFoundError("Branch", branch_id)
        return branch_id

    def create(self, data: UserCreate) -> UserModel:
        """Creates a user hashing the password; never persists it in plain text."""
        payload = data.model_dump(exclude={"password", "role", "branch_id"})
        user = UserModel(
            **payload,
            role=data.role.value,
            branch_id=self._normalize_branch(data.role.value, data.branch_id),
            hashed_password=hash_password(data.password),
        )
        return self._persist(user)

    def update(self, id: int, data: UserUpdate) -> UserModel:
        """Updates a user; rehashes ``password`` when it's included."""
        obj = self.get_or_404(id)
        changes = data.model_dump(exclude_unset=True)
        if "password" in changes:
            obj.hashed_password = hash_password(changes.pop("password"))
        if changes.get("role") is not None:
            changes["role"] = UserRole(changes["role"]).value
        for field, value in changes.items():
            setattr(obj, field, value)
        # Reconciles role<->branch over the resulting state (covers role and/or
        # branch changes, and normalizes the admin to a null branch).
        obj.branch_id = self._normalize_branch(obj.role, obj.branch_id)
        return self._persist(obj)

    def update_profile(self, user: UserModel, data: ProfileUpdate) -> UserModel:
        """Self-service: the user edits their own profile (``full_name`` only).

        Doesn't touch ``role``/``is_active``/``email``: that's management and lives
        in the admin-only CRUD. PATCH semantics: only applies the fields sent.
        """
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        return self._persist(user)

    def change_password(self, user: UserModel, current: str, new: str) -> None:
        """Self-service: changes the own password after verifying the current one.

        Raises ``AuthenticationError`` (401) if the current password doesn't match.
        The caller revokes the refresh tokens to force re-login on other devices.
        """
        if not verify_password(current, user.hashed_password):
            raise AuthenticationError("La contraseña actual es incorrecta")
        user.hashed_password = hash_password(new)
        self._persist(user)

    def get_by_email(self, email: str) -> Optional[UserModel]:
        """Gets a user by email (login identifier)."""
        return self.db.query(UserModel).filter(UserModel.email == email).first()

    def authenticate(self, email: str, password: str) -> Optional[UserModel]:
        """Validates credentials; returns the user if active and matching."""
        user = self.get_by_email(email)
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    def list_users(
        self,
        search: Optional[str] = None,
        roles: Optional[List[UserRole]] = None,
        branch_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        sort: ListSort = "name",
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[UserModel], int]:
        """Lists users with optional search, facets and ordering; ``(items, total)``.

        ``roles``, ``branch_id`` and ``is_active`` were all columns of the admin
        listing with no way to filter by them, on the one page where "show me the
        operators of this branch" is the question actually being asked.
        """
        query = self.db.query(UserModel)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                UserModel.email.ilike(pattern) | UserModel.full_name.ilike(pattern)
            )
        if roles:
            query = query.filter(UserModel.role.in_([r.value for r in roles]))
        if branch_id is not None:
            query = query.filter(UserModel.branch_id == branch_id)
        if is_active is not None:
            query = query.filter(UserModel.is_active.is_(is_active))
        query = self._apply_sort(query, sort, UserModel.full_name)
        return self._paginate(query, limit, offset)

    def list_by_roles(self, roles: List[UserRole]) -> List[UserModel]:
        """Active users whose role is one of ``roles`` (notification recipients)."""
        values = [role.value for role in roles]
        return (
            self.db.query(UserModel)
            .filter(UserModel.role.in_(values), UserModel.is_active.is_(True))
            .all()
        )

    def list_by_role_and_branch(
        self, role: UserRole, branch_id: int
    ) -> List[UserModel]:
        """Active users of ``role`` bound to ``branch_id`` (workshop recipients)."""
        return (
            self.db.query(UserModel)
            .filter(
                UserModel.role == role.value,
                UserModel.branch_id == branch_id,
                UserModel.is_active.is_(True),
            )
            .all()
        )


def user_service(db: Session = Depends(get_db)) -> UserService:
    """``UserService`` provider for route injection."""
    return UserService(db)
