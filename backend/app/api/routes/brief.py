"""今日简报。App 的第二个 tab。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import ApiResponse, BriefOut
from app.services.brief import BriefService

router = APIRouter(prefix="/brief", tags=["brief"])


@router.get("/today", response_model=ApiResponse[BriefOut])
async def today_brief(
    hours: int = Query(36, ge=6, le=240, description="回看最近多少小时的通知"),
    session: AsyncSession = Depends(get_session),
):
    service = BriefService(session)
    return ApiResponse.ok(await service.today(hours=hours))
