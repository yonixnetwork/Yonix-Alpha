from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from sqlalchemy import select

from app.api import review_cache
from app.api.router import api_router
from app.config_revision import ConfigRevisionMiddleware
from app.request_timing import RequestTimingMiddleware, request_id_of
from yonixalpha_core import config_validation
from yonixalpha_core.config import get_settings
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import User
from yonixalpha_core.db.redis import make_redis

log = get_logger("api.main")


async def _seed_admin_user(session_factory) -> None:
    settings = get_settings()
    if not settings.ADMIN_PASSWORD_HASH:
        log.warning("admin.seed.skipped", reason="ADMIN_PASSWORD_HASH not set")
        return
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.username == settings.ADMIN_USERNAME))
        existing = result.scalar_one_or_none()
        if existing is None:
            session.add(User(username=settings.ADMIN_USERNAME, password_hash=settings.ADMIN_PASSWORD_HASH))
            await session.commit()
            log.info("admin.seed.created", username=settings.ADMIN_USERNAME)
        elif existing.password_hash != settings.ADMIN_PASSWORD_HASH:
            # ADMIN_PASSWORD_HASH in the environment is the source of truth —
            # rotating it in .env and restarting is how the admin password changes.
            existing.password_hash = settings.ADMIN_PASSWORD_HASH
            await session.commit()
            log.info("admin.seed.password_rotated", username=settings.ADMIN_USERNAME)


async def _validate_configuration(session_factory, redis, settings) -> None:
    """Startup config validation per module (names of missing variables
    only, never values). A module in CONFIGURATION_ERROR is reported and
    cannot be switched to AUTO/LIVE; the others start normally."""
    try:
        async with session_factory() as session:
            result = await config_validation.load_and_validate(session, settings)
        await config_validation.store_result(redis, result)
    except Exception as exc:  # noqa: BLE001 - validation must never stop the API from starting
        log.warning("config.validation_failed", error=type(exc).__name__)
        return
    for name, r in result.items():
        if r["status"] == config_validation.CONFIG_ERROR:
            log.warning("config.module_error", module=name, errors=r["errors"])
        else:
            log.info("config.module", module=name, status=r["status"], live_missing=len(r["live_missing"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    # The dashboard API's own limits: a stuck statement is stopped before the
    # reverse proxy's 60 s and a full pool fails fast (yonixalpha_core.config).
    engine = make_engine(settings, statement_timeout_ms=settings.API_STATEMENT_TIMEOUT_MS,
                         pool_timeout_s=settings.API_POOL_TIMEOUT_S)
    session_factory = make_session_factory(engine)
    # The review aggregates run in the background on their own two connections
    # with a longer limit, never on the request pool (app.api.review_cache).
    review_engine = make_engine(settings, statement_timeout_ms=settings.API_REVIEW_STATEMENT_TIMEOUT_MS,
                                pool_timeout_s=settings.API_POOL_TIMEOUT_S, pool_size=1, max_overflow=1)
    redis = make_redis(settings)

    app.state.settings = settings
    app.state.engine = engine
    app.state.db_session_factory = session_factory
    app.state.review_session_factory = make_session_factory(review_engine)
    app.state.redis = redis
    # Outbound calls made by the API itself (provider TEST CONNECTION, EVM wallet balances).
    http = httpx.AsyncClient(headers={"User-Agent": "yonixalpha-api"})
    app.state.http = http

    await _seed_admin_user(session_factory)
    await _validate_configuration(session_factory, redis, settings)

    log.info("api.startup", app_env=settings.APP_ENV, trading_enabled=settings.TRADING_ENABLED)
    yield

    await review_cache.shutdown()
    await http.aclose()
    await engine.dispose()
    await review_engine.dispose()
    await redis.aclose()
    log.info("api.shutdown")


QUERY_CANCELED = "57014"  # Postgres query_canceled: statement_timeout reached


def _is_statement_timeout(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return code == QUERY_CANCELED or "statement timeout" in str(exc).lower()


def _unavailable(request: Request, code: str, detail: str) -> JSONResponse:
    rid = request_id_of(request.scope)
    return JSONResponse(status_code=503, content={"detail": f"{detail} (request {rid})", "code": code, "request_id": rid})


async def _db_error(request: Request, exc: sa_exc.DBAPIError) -> JSONResponse:
    if _is_statement_timeout(exc):
        secs = get_settings().API_STATEMENT_TIMEOUT_MS // 1000
        log.warning("api.query_timeout", path=request.url.path, request_id=request_id_of(request.scope))
        return _unavailable(request, "QUERY_TIMEOUT", f"The database query took longer than {secs} s and was stopped")
    log.error("api.db_error", path=request.url.path, error=type(getattr(exc, "orig", exc)).__name__,
              request_id=request_id_of(request.scope))
    return _unavailable(request, "DB_ERROR", "Database error")


async def _pool_timeout(request: Request, exc: sa_exc.TimeoutError) -> JSONResponse:
    log.warning("api.db_pool_exhausted", path=request.url.path, request_id=request_id_of(request.scope))
    return _unavailable(request, "DB_POOL_EXHAUSTED", "Every database connection is busy; try again shortly")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
    app.add_exception_handler(sa_exc.DBAPIError, _db_error)
    app.add_exception_handler(sa_exc.TimeoutError, _pool_timeout)

    # Innermost: runs after the route committed, before CORS headers are added.
    app.add_middleware(ConfigRevisionMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Config-Revision", "X-Request-ID", "X-Response-Time-Ms"],
    )
    # Outermost: times the whole request, including CORS and the error handlers.
    app.add_middleware(RequestTimingMiddleware, redis_getter=lambda scope: getattr(scope["app"].state, "redis", None))

    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
