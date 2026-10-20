"""路由聚合。新增一组接口只要在这里 include 一次。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    brief,
    chat,
    health,
    imports,
    ingest,
    notifications,
    push,
    sources,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(notifications.router)
api_router.include_router(sources.router)
api_router.include_router(ingest.router)
api_router.include_router(imports.router)
api_router.include_router(brief.router)
api_router.include_router(chat.router)
api_router.include_router(push.router)

__all__ = ["api_router"]
