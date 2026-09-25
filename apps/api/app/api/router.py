from fastapi import APIRouter

from app.api.routes import (
    analytics,
    auth,
    candidates,
    control,
    health,
    live,
    ml,
    notifications,
    paper,
    risk,
    signals,
    strategies,
    summary,
    system,
    tokens,
    venues,
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
api_router.include_router(analytics.router)
api_router.include_router(strategies.router)
api_router.include_router(venues.router)
api_router.include_router(summary.router)
api_router.include_router(notifications.router)
api_router.include_router(tokens.router)
api_router.include_router(ws.router)
