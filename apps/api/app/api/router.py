from fastapi import APIRouter

from app.api.routes import (
    analytics,
    auth,
    candidates,
    chains,
    config,
    control,
    copy,
    evm,
    explorer,
    health,
    live,
    ml,
    notifications,
    observations,
    paper,
    risk,
    rpc,
    settings_center,
    signals,
    strategies,
    summary,
    system,
    tokens,
    trade,
    wallets,
    ws,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(system.router)
api_router.include_router(candidates.router)
api_router.include_router(signals.router)
api_router.include_router(risk.router)
api_router.include_router(ml.router)
api_router.include_router(paper.router)
api_router.include_router(live.router)
api_router.include_router(control.router)
api_router.include_router(config.router)
api_router.include_router(rpc.router)
api_router.include_router(trade.router)
api_router.include_router(analytics.router)
api_router.include_router(strategies.router)
api_router.include_router(summary.router)
api_router.include_router(notifications.router)
api_router.include_router(tokens.router)
api_router.include_router(observations.router)
api_router.include_router(settings_center.router)
api_router.include_router(chains.router)
api_router.include_router(evm.router)
api_router.include_router(copy.router)
api_router.include_router(wallets.router)
api_router.include_router(explorer.router)
api_router.include_router(ws.router)
