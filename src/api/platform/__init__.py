"""平台 API 路由：auth / assets / events / admin 四域，挂载于 /api/v1。"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.platform.assets import router as assets_router
from src.api.platform.auth import router as auth_router
from src.api.platform.events import router as events_router
from src.api.platform.admin import router as admin_router

platform_router = APIRouter(prefix="/api/v1", tags=["platform"])
platform_router.include_router(auth_router)
platform_router.include_router(assets_router)
platform_router.include_router(events_router)
platform_router.include_router(admin_router)

__all__ = ["platform_router"]
