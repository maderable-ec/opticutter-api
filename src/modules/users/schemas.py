from datetime import datetime
from typing import Any, List, Optional

from pydantic import EmailStr, Field, model_validator

from src.modules.users.enums import UserRole
from src.shared.schemas import CamelModel


class UserBase(CamelModel):
    email: EmailStr = Field(..., description="Login email (unique)")
    full_name: Optional[str] = Field(None, max_length=128, description="Full name")
    branch_id: Optional[int] = Field(
        default=None,
        description=(
            "Assigned branch (required for vendedor/operador/canteador). The "
            "administrador is global: ignored and left null."
        ),
    )


class _LegacyRoleInput(CamelModel):
    """Accepts the single ``role`` of the previous web as ``roles=[role]``.

    Only while both contracts are live: the api ships first, and a web that
    still sends ``role`` must keep creating and editing users. Goes away with
    migration 014, together with ``UserResponse.role``.
    """

    @model_validator(mode="before")
    @classmethod
    def _role_to_roles(cls, data: Any) -> Any:
        # ``roles`` wins when both come; a null ``role`` was ignored and still is.
        if isinstance(data, dict) and "role" in data:
            data = dict(data)
            role = data.pop("role")
            if "roles" not in data and role is not None:
                data["roles"] = [role]
        return data


class UserCreate(_LegacyRoleInput, UserBase):
    """User creation. The password travels in plain text only here; it's hashed on persist."""

    roles: List[UserRole] = Field(
        default_factory=lambda: [UserRole.OPERATOR],
        min_length=1,
        description=(
            "Roles (the permissions are their union). Only operador and "
            "canteador combine; administrador and vendedor are exclusive."
        ),
    )
    password: str = Field(..., min_length=8, max_length=128, description="Password")


class UserUpdate(_LegacyRoleInput):
    """Partial update. Sending ``password`` rehashes it; ``isActive`` deactivates."""

    email: Optional[EmailStr] = Field(None, description="Login email (unique)")
    full_name: Optional[str] = Field(None, max_length=128, description="Full name")
    roles: Optional[List[UserRole]] = Field(
        None, min_length=1, description="Roles (replaces the whole list)"
    )
    is_active: Optional[bool] = Field(
        None, description="Active/inactive (logical deactivation)"
    )
    branch_id: Optional[int] = Field(
        None, description="Assigned branch (staff); null/ignored for administrador"
    )
    password: Optional[str] = Field(
        None, min_length=8, max_length=128, description="New password"
    )


class UserResponse(UserBase):
    """Public representation of a user. Never includes the password or its hash.

    Reads stored rows, so it inherits no validator of the input schemas.
    """

    id: int = Field(..., description="User ID")
    roles: List[UserRole] = Field(..., description="Roles, in canonical order")
    role: UserRole = Field(
        ...,
        description=(
            "DEPRECATED: the primary role (first of ``roles``). Kept for the "
            "previous web; removed with migration 014."
        ),
    )
    is_active: bool = Field(..., description="Active/inactive")
    created_at: datetime = Field(..., description="Creation date")


class ProfileUpdate(CamelModel):
    """Self-service: the user edits their own profile (``fullName`` only)."""

    full_name: Optional[str] = Field(None, max_length=128, description="Full name")


class ChangePasswordRequest(CamelModel):
    """Self-service: own password change after verifying the current one."""

    current_password: str = Field(..., description="Current password")
    new_password: str = Field(
        ..., min_length=8, max_length=128, description="New password"
    )


class LoginRequest(CamelModel):
    email: EmailStr = Field(..., description="Login email")
    password: str = Field(..., description="Password")


class RefreshRequest(CamelModel):
    """Exchanges the refresh token for a new pair on ``/auth/refresh``."""

    refresh_token: str = Field(..., description="Opaque refresh token issued at login")


class TokenResponse(CamelModel):
    """Login/refresh response: token pair + authenticated user data."""

    access_token: str = Field(..., description="Access JWT (short-lived)")
    refresh_token: str = Field(
        ..., description="Opaque refresh token (long, revocable)"
    )
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Access token validity in seconds")
    user: UserResponse = Field(..., description="Authenticated user")
