from fastapi import APIRouter

from app.api.routes import auth, candidates, control, health, ml, paper, risk, signals, system

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(system.router)
api_router.include_router(candidates.router)
api_router.include_router(signals.router)
api_router.include_router(risk.router)
api_router.include_router(ml.router)
api_router.include_router(paper.router)
api_router.include_router(control.router)
