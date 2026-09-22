import uuid
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from yonixalpha_core.config import Settings
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.security import create_token, decode_token, verify_password
from yonixalpha_core.db.models import AuditLog, Session as SessionModel, User
from app.schemas.auth import LoginRequest, MeResponse, RefreshRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("api.auth")

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
ATTEMPT_WINDOW_SECONDS = 15 * 60


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _is_locked_out(redis: Redis, username: str) -> bool:
    return bool(await redis.get(f"auth:lockout:{username}"))


async def _record_failed_attempt(redis: Redis, username: str, settings: Settings, ip: str | None) -> None:
    key = f"auth:failed:{username}"
    attempts = await redis.incr(key)
    if attempts == 1:
        await redis.expire(key, ATTEMPT_WINDOW_SECONDS)
    if attempts >= MAX_FAILED_ATTEMPTS:
        await redis.set(f"auth:lockout:{username}", "1", ex=LOCKOUT_SECONDS)
        # Only on the transition into lockout, not every attempt after —
        # a real brute-force run would otherwise spam this channel for the
        # full lockout window.
        await send_telegram_alert(
            settings,
            f"⚠️ Login lockout: '{username}' locked out for {LOCKOUT_SECONDS // 60}m "
            f"after {attempts} failed attempts from {ip or 'unknown IP'}",
        )


async def _clear_failed_attempts(redis: Redis, username: str) -> None:
    await redis.delete(f"auth:failed:{username}", f"auth:lockout:{username}")


async def _write_audit(db: AsyncSession, user_id: uuid.UUID | None, event_type: str, ip: str | None, detail: dict | None = None) -> None:
    db.add(AuditLog(user_id=user_id, event_type=event_type, ip_address=ip, detail=detail))
    await db.commit()


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    ip = _client_ip(request)

    if await _is_locked_out(redis, body.username):
        log.warning("auth.login.locked_out", username=body.username, ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
        )

    result = await db.execute(select(User).where(User.username == body.username, User.is_active.is_(True)))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        await _record_failed_attempt(redis, body.username, settings, ip)
        await _write_audit(db, None, "login_failed", ip, {"username": body.username})
        log.warning("auth.login.failed", username=body.username, ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    await _clear_failed_attempts(redis, body.username)

    access_token, _, access_expires = create_token(settings, subject=user.username, token_type="access")
    refresh_token, refresh_jti, refresh_expires = create_token(settings, subject=user.username, token_type="refresh")

    db.add(
        SessionModel(
            user_id=user.id,
            refresh_token_jti=refresh_jti,
            expires_at=refresh_expires,
            user_agent=request.headers.get("user-agent"),
            ip_address=ip,
        )
    )
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await _write_audit(db, user.id, "login", ip, {"username": user.username})
    log.info("auth.login.success", username=user.username, ip=ip)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    try:
        payload = decode_token(settings, body.refresh_token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong token type")

    jti = payload["jti"]
    result = await db.execute(select(SessionModel).where(SessionModel.refresh_token_jti == jti))
    session_row = result.scalar_one_or_none()

    if session_row is None or session_row.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked or not found")
    if session_row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    # Rotate: revoke the used refresh token, issue a fresh pair. Limits the
    # blast radius of a leaked refresh token to a single use.
    session_row.revoked_at = datetime.now(timezone.utc)

    access_token, _, _ = create_token(settings, subject=payload["sub"], token_type="access")
    new_refresh_token, new_jti, new_refresh_expires = create_token(settings, subject=payload["sub"], token_type="refresh")

    db.add(
        SessionModel(
            user_id=session_row.user_id,
            refresh_token_jti=new_jti,
            expires_at=new_refresh_expires,
            user_agent=session_row.user_agent,
            ip_address=session_row.ip_address,
        )
    )
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    try:
        payload = decode_token(settings, body.refresh_token)
    except jwt.PyJWTError:
        return  # already invalid — logout is idempotent

    result = await db.execute(select(SessionModel).where(SessionModel.refresh_token_jti == payload.get("jti")))
    session_row = result.scalar_one_or_none()
    if session_row is not None and session_row.revoked_at is None:
        session_row.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        await _write_audit(db, session_row.user_id, "logout", session_row.ip_address)


@router.get("/me", response_model=MeResponse)
async def me(username: str = Depends(get_current_username)) -> MeResponse:
    return MeResponse(username=username)
