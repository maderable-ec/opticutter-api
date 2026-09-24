"""Unit: pure security primitives (no DB, no mocks).

``hash_token``/``generate_refresh_token`` and the JWT ``create``/``decode`` cycle
are pure functions (they only read ``config``), ideal for deterministic unit tests.
Password hashing (bcrypt) is already exercised in the auth integration tests.
"""

import pytest

from src.shared.exceptions import AuthenticationError
from src.shared.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_token,
)


def test_hash_token_is_deterministic_hex():
    assert hash_token("abc123") == hash_token("abc123")
    assert hash_token("abc123") != hash_token("abc124")
    assert len(hash_token("abc123")) == 64  # sha256 in hex


def test_access_token_round_trip_carries_subject_and_roles():
    token = create_access_token(42, ["administrador"])
    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["roles"] == ["administrador"]


def test_expired_token_raises_authentication_error():
    token = create_access_token(1, ["operador"], expires_minutes=-1)
    with pytest.raises(AuthenticationError):
        decode_access_token(token)


def test_tampered_token_raises_authentication_error():
    token = create_access_token(1, ["operador"])
    with pytest.raises(AuthenticationError):
        decode_access_token(token + "tampered")


def test_refresh_tokens_are_unique():
    assert generate_refresh_token() != generate_refresh_token()
