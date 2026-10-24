"""健康检查。App 启动时用它判断「后端在线 / 离线演示模式」。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import available_kinds
from app.core.config import get_settings
from app.db import get_session
from app.models import Notification, Source
from app.schemas import ApiResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[dict])
async def health(
    deep: bool = Query(False, description="true 时额外做一次 AI 连通性自检"),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()

    db_ok = True
    db_error = None
    source_count = notif_count = pending = unread = pinned = 0
    try:
        source_count = int(
            (await session.execute(select(func.count()).select_from(Source))).scalar() or 0
        )
        notif_count = int(
            (await session.execute(select(func.count()).select_from(Notification))).scalar() or 0
        )
        pending = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.ai_status == "pending")
                )
            ).scalar()
            or 0
        )
        unread = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.is_read.is_(False))
                )
            ).scalar()
            or 0
        )
        pinned = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.is_pinned.is_(True))
                )
            ).scalar()
            or 0
        )
    except Exception as e:  # noqa: BLE001
        db_ok = False
        db_error = str(e)[:300]

    from app.workers.scheduler import scheduler_status

    # 推送概览。它要查库，所以包一层：health 是「后端到底活着没」的探测点，
    # 绝不能被推送统计的异常搞成 500 —— 那样客户端会误判成后端挂了。
    try:
        from app.services.push import PushService

        push_info = await PushService(session, settings).stats()
        push_info.update(
            {
                "enabled": settings.push_enabled,
                "channels": settings.push_channel_list(),
            }
        )
        push_info.pop("baseline_at", None)
    except Exception as e:  # noqa: BLE001
        push_info = {"error": str(e)[:200]}

    # 「现在到底用哪个模型」必须报**生效中的**那份，不是 .env 那份。
    # 之前这里读 settings.ai_model：用户切到自定义模型后，health 还一直说
    # glm-4.7-flash，看着像「切换没生效」——而实际已经在跑新模型了。
    # 这类假报告比没报告更坏，排障时会把方向带反。
    from app.services.llm_config import get_active

    ai_cfg = get_active(settings)
    data = {
        "app": settings.app_name,
        "env": settings.env,
        "database": {
            "ok": db_ok,
            "dialect": settings.database_url.split("+")[0].split(":")[0],
            "error": db_error,
        },
        "ai": {
            "enabled": ai_cfg.enabled,
            "model": ai_cfg.model,
            "name": ai_cfg.name,
            "source": ai_cfg.source,
            "endpoint": ai_cfg.endpoint,
        },
        "counts": {
            "sources": source_count,
            "notifications": notif_count,
            "ai_pending": pending,
            "unread": unread,
            "pinned": pinned,
        },
        "adapters": available_kinds(),
        "scheduler": scheduler_status(),
        "cron": settings.refresh_cron(),
        "push": push_info,
    }

    if deep:
        from app.services.ai_client import check_ai

        data["ai"]["check"] = await check_ai(settings, ai_cfg)

    return ApiResponse.ok(data)
