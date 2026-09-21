from fastapi import APIRouter, Depends

from app.api.deps import get_current_username, get_settings
from app.core.config import Settings

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def status(
    _: str = Depends(get_current_username),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Overview panel data per docs/API.md — real engine/connection status
    fields are added as each engine is wired up in Phase 2+; today this
    reports application-level state only, never fabricated numbers.
    """
    return {
        "app_env": settings.APP_ENV,
        "app_name": settings.APP_NAME,
        "trading_enabled": settings.TRADING_ENABLED,
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
        "engines": {
            "solana_discovery": "not_implemented",
            "solana_migration": "not_implemented",
            "solana_momentum": "not_implemented",
            "binance_futures": "not_implemented",
        },
    }
