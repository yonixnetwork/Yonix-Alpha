import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from yonixalpha_core.config import Settings

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        # Malformed/unset hash (e.g. ADMIN_PASSWORD_HASH left blank) — never
        # treat as a match, never raise past the caller as a 500.
        return False


def create_token(
    settings: Settings,
    subject: str,
    token_type: TokenType,
    jti: str | None = None,
) -> tuple[str, str, datetime]:
    """Returns (encoded_jwt, jti, expires_at)."""
    now = datetime.now(timezone.utc)
    ttl = (
        timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
        if token_type == "access"
        else timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    )
    expires_at = now + ttl
    jti = jti or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": expires_at,
        "jti": jti,
    }
    encoded = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded, jti, expires_at


def decode_token(settings: Settings, token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError on any invalid/expired/malformed token."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
