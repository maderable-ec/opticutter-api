from fastapi import APIRouter, Depends, Request

from src.modules.users.dependencies import get_current_user
from src.modules.users.login_event_service import (
    LoginEventService,
    login_event_service,
)
from src.modules.users.model import UserModel
from src.modules.users.refresh_token_service import (
    RefreshTokenService,
    refresh_token_service,
)
from src.modules.users.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    ProfileUpdate,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from src.modules.users.service import UserService, user_service
from src.shared.config import config
from src.shared.exceptions import AuthenticationError
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok
from src.shared.security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)


def _token_response(user: UserModel, refresh_token: str) -> TokenResponse:
    """Builds the token pair (access JWT + refresh) and the user's data."""
    return TokenResponse(
        access_token=create_access_token(user.id, user.roles),
        refresh_token=refresh_token,
        expires_in=config.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.post("/login", response_model=DataResponse[TokenResponse])
def login(
    data: LoginRequest,
    request: Request,
    svc: UserService = Depends(user_service),
    refresh_svc: RefreshTokenService = Depends(refresh_token_service),
    login_event_svc: LoginEventService = Depends(login_event_service),
):
    """Validates email + password and issues an access/refresh pair."""
    user = svc.authenticate(data.email, data.password)
    if user is None:
        # Generic message: doesn't reveal whether the email exists or it was the password.
        raise AuthenticationError("Email o contraseña incorrectos")
    # Records the entry ("arrival time" reference); only on login, not refresh.
    login_event_svc.record(
        user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(_token_response(user, refresh_svc.issue(user.id)))


@router.post("/refresh", response_model=DataResponse[TokenResponse])
def refresh(
    data: RefreshRequest,
    refresh_svc: RefreshTokenService = Depends(refresh_token_service),
):
    """Exchanges a refresh token for a new pair (rotates the presented one)."""
    user, new_refresh = refresh_svc.rotate(data.refresh_token)
    return ok(_token_response(user, new_refresh))


@router.post("/logout", status_code=204)
def logout(
    data: RefreshRequest,
    refresh_svc: RefreshTokenService = Depends(refresh_token_service),
):
    """Closes the session by revoking the presented refresh token (idempotent)."""
    refresh_svc.revoke(data.refresh_token)


@router.get("/me", response_model=DataResponse[UserResponse])
def me(current_user: UserModel = Depends(get_current_user)):
    """Returns the authenticated user."""
    return ok(current_user)


@router.patch("/me", response_model=DataResponse[UserResponse])
def update_me(
    data: ProfileUpdate,
    current_user: UserModel = Depends(get_current_user),
    svc: UserService = Depends(user_service),
):
    """Self-service: the user edits their own profile (``fullName`` only)."""
    return ok(svc.update_profile(current_user, data))


@router.post("/change-password", status_code=204)
def change_password(
    data: ChangePasswordRequest,
    current_user: UserModel = Depends(get_current_user),
    svc: UserService = Depends(user_service),
    refresh_svc: RefreshTokenService = Depends(refresh_token_service),
):
    """Self-service: changes the own password and revokes open sessions."""
    svc.change_password(current_user, data.current_password, data.new_password)
    refresh_svc.revoke_all_for_user(current_user.id)
