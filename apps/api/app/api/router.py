from fastapi import APIRouter

from app.api.routes import auth, health, system

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(system.router)
