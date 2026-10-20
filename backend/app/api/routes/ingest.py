"""抓取入口。App 下拉刷新、调试、定时任务都走这里。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import get_session
from app.models import FetchLog
from app.schemas import ApiResponse
from app.services.ingest import IngestService

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/run", response_model=ApiResponse[dict])
async def run_ingest(
    source_ids: str | None = Query(None, description="逗号分隔的来源 id，留空表示全部启用的来源"),
    summarize: bool | None = Query(None, description="是否顺带跑 AI 摘要，留空按配置"),
    notify: bool = Query(
        True,
        description="抓完是否顺带推送新条目。重复刷新不会重复推 —— 推送有 notified_at 去重。",
    ),
    session: AsyncSession = Depends(get_session),
):
    ids = None
    if source_ids:
        ids = [int(x) for x in source_ids.split(",") if x.strip().isdigit()]

    service = IngestService(session)
    result = await service.run(source_ids=ids, do_summarize=summarize)

    data = result.model_dump()
    if notify:
        # 推送失败不能把「抓取成功」变成失败 —— 消息已经入库了，这是事实。
        from app.services.push import PushService

        try:
            data["push"] = await PushService(session).push_pending()
        except Exception as e:  # noqa: BLE001
            data["push"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return ApiResponse.ok(data)


@router.get("/logs", response_model=ApiResponse[list[dict]])
async def recent_logs(
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        (
            await session.execute(
                select(FetchLog).order_by(FetchLog.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        [
            {
                "id": r.id,
                "source_id": r.source_id,
                "source_name": r.source_name,
                "status": r.status,
                "item_count": r.item_count,
                "new_count": r.new_count,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in rows
        ]
    )


@router.get("/config", response_model=ApiResponse[dict])
async def ingest_config():
    s = get_settings()
    return ApiResponse.ok(
        {
            "cron": s.refresh_cron(),
            "timezone": s.timezone,
            "enable_scheduler": s.enable_scheduler,
            "respect_robots": s.respect_robots,
            "fetch_concurrency": s.fetch_concurrency,
            "fetch_min_interval": s.fetch_min_interval,
            "auto_summarize": s.auto_summarize,
            "push": {
                "enabled": s.push_enabled,
                "channels": s.push_channel_list(),
                "lookback_hours": s.push_lookback_hours,
                "min_importance": s.push_min_importance,
                "quiet_hours": s.push_quiet_hours or None,
            },
        }
    )
