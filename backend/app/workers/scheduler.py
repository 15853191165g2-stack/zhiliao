"""定时刷新 + 推送。

阶段 3 用 APScheduler 跑通「每天两次」；量与并发上来后换 RQ / Celery，
只需替换本文件的 `start_scheduler()`，业务代码一行不用动。

一次 job 干三件事，顺序不能换：
    抓取 → 摘要 → 推送
推的必须是「已经有过摘要」的条目（`ai_status='done'`），
否则用户收到的通知里没有 3 行摘要，跟需求里的产品形态对不上。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_last_run: dict[str, object] = {
    "started_at": None,
    "finished_at": None,
    "status": "idle",
}


async def job_ingest() -> dict:
    """定时任务体：抓取全部启用的来源 → 生成摘要 → 推送新条目。"""
    from app.core.timeutil import now_utc
    from app.db import SessionLocal
    from app.services.ingest import IngestService
    from app.services.push import PushService

    _last_run["started_at"] = now_utc().isoformat()
    _last_run["status"] = "running"
    try:
        async with SessionLocal() as session:
            service = IngestService(session)
            result = await service.run()
            logger.info(
                "定时刷新完成：抓取 %d 条，新增 %d 条，更新 %d 条，摘要 %d 条，耗时 %.0fms",
                result.total_items, result.new_items, result.updated_items,
                result.summarized, result.duration_ms,
            )
            _last_run["result"] = {
                "total_items": result.total_items,
                "new_items": result.new_items,
                "updated_items": result.updated_items,
                "summarized": result.summarized,
            }

            # ---- 推送 ----
            # ⚠️ 推送失败**不能**让整个 job 变成 error。
            #    抓取入库是主线（消息已经进来了），推送是外部动作 ——
            #    通道挂了就挂一条日志，下一轮还会重试（notified_at 没被标记）。
            push_stats: dict = {}
            try:
                push_stats = await PushService(session).push_pending()
                logger.info(
                    "推送完成：候选 %d，送达 %d，失败 %d，通道 %s",
                    push_stats.get("checked", 0), push_stats.get("sent", 0),
                    push_stats.get("failed", 0), push_stats.get("channels") or "无",
                )
                for w in push_stats.get("warnings") or []:
                    logger.info("推送提示：%s", w)
            except Exception as e:  # noqa: BLE001
                logger.exception("推送阶段失败（不影响抓取入库）")
                push_stats = {"error": f"{type(e).__name__}: {e}"[:300]}
            _last_run["push"] = push_stats

        _last_run["status"] = "ok"
    except Exception as e:  # noqa: BLE001
        logger.exception("定时刷新失败")
        _last_run["status"] = f"error: {e}"[:200]
    finally:
        from app.core.timeutil import now_utc

        _last_run["finished_at"] = now_utc().isoformat()
    return dict(_last_run)


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    settings = get_settings()
    hours = ",".join(h.strip() for h in settings.refresh_cron_hours.split(",") if h.strip())

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        job_ingest,
        trigger=CronTrigger(
            hour=hours,
            minute=settings.refresh_cron_minute,
            timezone=settings.timezone,
        ),
        id="quanzhi_ingest",
        name="抓取校园通知",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "定时任务已注册：每天 %s 点 %02d 分（%s）",
        settings.refresh_cron_hours, settings.refresh_cron_minute, settings.timezone,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
    _scheduler = None


def scheduler_status() -> dict:
    """给 /api/health 用。"""
    settings = get_settings()
    jobs = []
    if _scheduler is not None and _scheduler.running:
        for job in _scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
    return {
        "enabled": settings.enable_scheduler,
        "running": bool(_scheduler and _scheduler.running),
        "cron": settings.refresh_cron(),
        "timezone": settings.timezone,
        "jobs": jobs,
        "last_run": _last_run,
    }


async def run_now() -> dict:
    """手动触发一次（给脚本用）。"""
    await job_ingest()
    return dict(_last_run)
