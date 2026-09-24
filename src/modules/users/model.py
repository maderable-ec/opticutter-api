from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, validates

from src.modules.users.enums import GLOBAL_ROLES, UserRole, canonical_roles
from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class UserModel(TimestampMixin, AuditMixin, Base):
    """Internal system user (staff): credentials + roles.

    Login is by ``email`` (unique). The password is never stored in plain text:
    only its bcrypt hash in ``hashed_password``. Deactivation is logical
    (``is_active``) to avoid breaking references or losing traceability.

    A user holds one or more ``roles`` and is granted the UNION of their
    permissions. Only the workshop roles combine (a bander learning to cut is
    ``operador`` + ``canteador``); ``UserService`` enforces that, not the table,
    so the rule can be relaxed without a migration.
    """

    __tablename__ = "users"
    __table_args__ = (
        # Named per NAMING_CONVENTION: the final name is ``ck_users_roles_not_empty``.
        CheckConstraint("cardinality(roles) >= 1", name="roles_not_empty"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(128))
    hashed_password: Mapped[str] = mapped_column(String(255))
    # Canonical order (``canonical_roles``), so ``roles[0]`` is the primary role.
    # An ARRAY does not track in-place mutation: always assign a new list.
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    # LEGACY mirror of the primary role, written only by ``_canonical_roles``.
    # Nothing reads it any more; it survives so the previous release -- which
    # does -- keeps working if the api is rolled back. Dropped by migration 014.
    role: Mapped[str] = mapped_column(String(32), default=UserRole.OPERATOR.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Branch assigned to staff (seller/operator). NULL = global administrator,
    # who sees and operates all branches. Editable by the admin (moves the branch);
    # the change takes effect instantly (the branch doesn't travel in the JWT).
    branch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("branches.id"), index=True, nullable=True
    )

    @validates("roles")
    def _canonical_roles(self, _key, values):
        """Stores the roles in canonical order and keeps the legacy mirror in step."""
        ordered = canonical_roles(values)
        if ordered:
            self.role = ordered[0]
        return ordered

    def has_any_role(self, *roles: UserRole) -> bool:
        """Does the user hold at least one of ``roles``? (permissions are a union)."""
        return any(role.value in self.roles for role in roles)

    @property
    def is_global(self) -> bool:
        """Global users (admin/seller) see and operate every branch."""
        return self.has_any_role(*GLOBAL_ROLES)
