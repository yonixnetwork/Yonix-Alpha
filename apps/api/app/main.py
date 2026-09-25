from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.router import api_router
from yonixalpha_core import config_validation
from yonixalpha_core.config import get_settings
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import User
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.venues.registry import build_venues

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

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)

    app.state.settings = settings
    app.state.engine = engine
    app.state.db_session_factory = session_factory
    app.state.redis = redis
    # Public market data / read-only account calls for the venue pages.
    http = httpx.AsyncClient(headers={"User-Agent": "yonixalpha-api"})
    app.state.http = http
    app.state.venues = build_venues(http, settings)

    await _seed_admin_user(session_factory)
    await _validate_configuration(session_factory, redis, settings)

    log.info("api.startup", app_env=settings.APP_ENV, trading_enabled=settings.TRADING_ENABLED)
    yield

    await http.aclose()
    await engine.dispose()
    await redis.aclose()
    log.info("api.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
