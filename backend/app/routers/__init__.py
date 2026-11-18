from fastapi import APIRouter

from app.routers import core, hotcold

api = APIRouter()
api.include_router(core.router)
api.include_router(hotcold.router)
