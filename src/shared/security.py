"""Security primitives: password hashing and JWT.

Infrastructure with no FastAPI/SQLAlchemy dependencies. The users module
(``src/modules/users``) builds login and the ``get_current_user`` dependency
on top of these helpers. Reads its configuration (``SECRET_KEY``, algorithm
and lifetimes) from ``shared.config``.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import bcrypt
import jwt

from src.shared.config import config
from src.shared.exceptions import AuthenticationError

# bcrypt operates on at most 72 bytes: we truncate so long passwords don't
# error out or change behavior across library versions.
_BCRYPT_MAX_BYTES = 72


def _encode_password(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    """Returns the bcrypt hash of a plaintext password (storable as text)."""
    salt = bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_encode_password(plain), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Compares a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(_encode_password(plain), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash (corrupted data): treat it as a non-match.
        return False


def create_access_token(
    subject: str | int, roles: Sequence[str], expires_minutes: Optional[int] = None
) -> str:
    """Issues a JWT signed with ``sub`` (user id), ``roles``, ``iat`` and ``exp``.

    ``roles`` is informational: authorization reads the live roles from the DB.
    """
    if isinstance(roles, str):
        # A bare string is a Sequence[str] too, and would land as its letters.
        raise TypeError("roles must be a list of role values, not a string")
    minutes = (
        expires_minutes
        if expires_minutes is not None
        else config.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "roles": list(roles),
        "iss": config.JWT_ISSUER,
        "aud": config.JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decodes and validates a JWT; raises ``AuthenticationError`` if invalid.

    Validates the signature, expiry, issuer and audience: a token minted for
    another audience/issuer (even with the same secret) is rejected.
    """
    try:
        return jwt.decode(
            token,
            config.SECRET_KEY,
            algorithms=[config.JWT_ALGORITHM],
            audience=config.JWT_AUDIENCE,
            issuer=config.JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("El token de sesión expiró") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Token de sesión inválido") from exc


def generate_refresh_token() -> str:
    """Generates an opaque 256-bit refresh token (CSPRNG).

    This is the credential that ``/auth/refresh`` exchanges for a new access
    token. Opaque (not a JWT) so it can be revoked: only its ``hash_token`` is
    persisted at rest.
    """
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """sha256 hex digest of an opaque token. The hash is persisted, never the
    raw token.

    A random 256-bit token is its own salt, so unsalted sha256 is enough (same
    criterion as the pre-order review links).
    """
    return hashlib.sha256(raw.encode()).hexdigest()
