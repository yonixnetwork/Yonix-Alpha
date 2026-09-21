import jwt
import pytest

from yonixalpha_core.config import Settings
from yonixalpha_core.security import create_token, decode_token, hash_password, verify_password


def _settings(**overrides) -> Settings:
    defaults = dict(
        JWT_SECRET="unit-test-secret-unit-test-secret-32",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD_HASH="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/15",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_password_hash_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_verify_password_rejects_malformed_hash():
    assert verify_password("anything", "not-a-real-hash") is False


def test_create_and_decode_access_token():
    settings = _settings()
    token, jti, expires_at = create_token(settings, subject="admin", token_type="access")
    payload = decode_token(settings, token)
    assert payload["sub"] == "admin"
    assert payload["type"] == "access"
    assert payload["jti"] == jti


def test_decode_token_rejects_wrong_secret():
    settings = _settings()
    token, _, _ = create_token(settings, subject="admin", token_type="access")
    wrong_settings = _settings(JWT_SECRET="a-completely-different-secret-32-chars")
    with pytest.raises(jwt.PyJWTError):
        decode_token(wrong_settings, token)


def test_jwt_secret_too_short_rejected():
    with pytest.raises(ValueError):
        _settings(JWT_SECRET="too-short")
