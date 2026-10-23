"""定时刷新 + 推送。

阶段 3 用 APScheduler 跑通「每天两次」；量与并发上来后换 RQ / Celery，
只需替换本文件的 `start_scheduler()`，业务代码一行不用动。

一次 job 干三件事，顺序不能换：
    抓取 → 摘要 → 推送
推的必须是「已经有过摘要」的条目（`ai_status='done'`），
否则用户收到的通知里没有 3 行摘要，跟需求里的产品形态对不上。
"""

from __future__ import annotations

import asyncio
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

# 一轮抓取在跑时不许再进来。现在有三条路径可能同时调 job_ingest：
# 定时触发、启动补抓（catchup_if_stale）、整轮失败后的自动重试。
# 撞上会重复写库、重复调模型（GLM 免费档并发只有 1，撞上直接 429）。
_ingesting = False
_retry_pending = False


def _failure_counts(result) -> tuple[int, int]:
    """返回 (失败来源数, 来源总数)。"""
    rows = getattr(result, "sources", None) or []
    total = len(rows)
    failed = sum(1 for s in rows if str(s.get("status") or "") == "error")
    return failed, total


def should_retry_total_failure(failed: int, total: int, enabled: bool = True) -> bool:
    """整轮全挂时该不该自动重试。

    ⚠️ `total > 0` 这个条件不能省。写成 `failed == total` 的话，
    「一个启用来源都没有」时 0 == 0 成立，会被判成整轮失败 → 触发重试 →
    重试又失败 → 无限重试。这种错只在没配来源时出现，平时全绿，最难查。
    """
    return bool(enabled) and total > 0 and failed == total


async def _delayed_retry(delay: int) -> None:
    """整轮抓取全部失败后，延迟重跑一次。

    为什么必须有这个：2026-10-23 18:00 那一轮，24 个来源**全部**
    `[Errno 11001] getaddrinfo failed` —— 本机 DNS 瞬时失效。
    一条都没抓进来，界面上完全看不出来，用户只觉得「今天没更新」。
    DNS / 断网这类故障通常几十秒内自愈，等一会儿重跑就能救回一整天。
    """
    global _retry_pending
    try:
        await asyncio.sleep(delay)
        logger.warning("整轮抓取失败 → 延迟 %d 秒后重试一次", delay)
        await job_ingest(allow_retry=False)
    except asyncio.CancelledError:  # 进程退出时正常取消
        raise
    except Exception:  # noqa: BLE001
        logger.exception("整轮失败后的重试也没成功（下一轮定时任务会继续）")
    finally:
        _retry_pending = False


def _schedule_retry() -> None:
    global _retry_pending
    settings = get_settings()
    if not settings.refresh_retry_on_total_failure:
        logger.warning("整轮抓取失败，但 REFRESH_RETRY_ON_TOTAL_FAILURE=false，不重试")
        return
    if _retry_pending:
        logger.info("已有一次重试在排队，不重复安排")
        return
    delay = max(10, int(settings.refresh_retry_delay_seconds))
    _retry_pending = True
    logger.warning("整轮抓取失败：%d 秒后自动重试一次（多为本机 DNS / 网络瞬时故障）", delay)
    asyncio.create_task(_delayed_retry(delay))


def should_catchup(last_ingest_at, now, threshold_hours: float) -> bool:
    """启动时是否该补抓。纯函数，便于单测。

    `last_ingest_at` / `now` 都是 naive UTC；`last_ingest_at` 为 None 表示从没抓过。
    """
    if threshold_hours <= 0:
        return False
    if last_ingest_at is None:
        return True
    return (now - last_ingest_at).total_seconds() / 3600.0 >= threshold_hours


async def catchup_if_stale() -> dict:
    """启动时发现数据陈旧就补抓一轮。

    为什么必须有这个：调度器是 **进程内** 的（AsyncIOScheduler + 内存 job store）。
    程序关掉、电脑关机期间错过的时间点，APScheduler **不会**回补 ——
    它只等下一个 cron 触发点。所以「每天 8 点 / 18 点自动更新」这句承诺，
    在「白天不开机」的情况下根本不成立。启动时对一下时间就能堵住这个洞。
    """
    settings = get_settings()
    if not settings.refresh_catchup_on_start:
        logger.info("启动补抓已关闭（REFRESH_CATCHUP_ON_START=false）")
        return {"ran": False, "reason": "disabled"}

    from sqlalchemy import func, select

    from app.core.timeutil import now_utc
    from app.db import SessionLocal
    from app.models import Source

    try:
        async with SessionLocal() as session:
            last = (
                await session.execute(
                    select(func.max(Source.last_fetched_at)).where(Source.enabled.is_(True))
                )
            ).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning("启动补抓检查失败（不影响启动）：%s", e)
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"[:120]}

    now = now_utc()
    threshold = settings.refresh_catchup_after_hours
    if not should_catchup(last, now, threshold):
        age = "从未抓取" if last is None else f"{((now - last).total_seconds() / 3600):.1f} 小时前"
        logger.info("启动补抓检查：上次抓取 %s，未超过 %.1f 小时阈值，跳过", age, threshold)
        return {"ran": False, "reason": "fresh", "age": age}

    age = "从未抓取" if last is None else f"{((now - last).total_seconds() / 3600):.1f} 小时前"
    logger.warning("启动补抓：上次抓取是 %s（超过 %.1f 小时），现在补跑一轮", age, threshold)
    await job_ingest()
    return {"ran": True, "age": age}


async def job_ingest(*, allow_retry: bool = True) -> dict:
    """定时任务体：抓取全部启用的来源 → 生成摘要 → 推送新条目。"""
    global _ingesting
    from app.core.timeutil import now_utc
    from app.db import SessionLocal
    from app.services.ingest import IngestService
    from app.services.push import PushService

    if _ingesting:
        logger.info("定时刷新跳过：上一轮仍在进行")
        return {**dict(_last_run), "skipped": "busy"}
    _ingesting = True

    _last_run["started_at"] = now_utc().isoformat()
    _last_run["status"] = "running"
    failed = total = 0
    try:
        async with SessionLocal() as session:
            service = IngestService(session)
            result = await service.run()
            logger.info(
                "定时刷新完成：抓取 %d 条，新增 %d 条，更新 %d 条，摘要 %d 条，耗时 %.0fms",
                result.total_items, result.new_items, result.updated_items,
                result.summarized, result.duration_ms,
            )
            failed, total = _failure_counts(result)
            _last_run["result"] = {
                "total_items": result.total_items,
                "new_items": result.new_items,
                "updated_items": result.updated_items,
                "summarized": result.summarized,
                "sources_failed": failed,
                "sources_total": total,
            }

            # ---- 整轮全挂：显式报错 + 延迟重试 ----
            # 不看这个数字的话，DNS 抖一下就会「安静地」一整天没数据。
            if should_retry_total_failure(failed, total):
                logger.error(
                    "整轮抓取全部失败：%d/%d 个来源出错，本次没有新数据入库。"
                    "最常见原因是本机 DNS 或网络不通（报错多为 getaddrinfo failed）。",
                    failed, total,
                )
                if allow_retry:
                    _schedule_retry()

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

        _last_run["status"] = "all_failed" if should_retry_total_failure(failed, total, True) else "ok"
    except Exception as e:  # noqa: BLE001
        logger.exception("定时刷新失败")
        _last_run["status"] = f"error: {e}"[:200]
    finally:
        from app.core.timeutil import now_utc

        _ingesting = False
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
