"""FastAPI 应用入口。

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router
from app.core.config import get_settings
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("quanzhi")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表
    await init_db()

    # 首次启动时把 sources/*.yaml 同步进库，让 App 一打开就有东西可看
    from app.db import SessionLocal
    from app.models import Source
    from app.services.source_config import sync_yaml_to_db
    from sqlalchemy import func, select

    try:
        async with SessionLocal() as session:
            count = int(
                (await session.execute(select(func.count()).select_from(Source))).scalar() or 0
            )
            if count == 0:
                stats = await sync_yaml_to_db(session)
                logger.info("首次启动，已从 yaml 导入来源：%s", stats)
    except Exception as e:  # noqa: BLE001
        logger.warning("同步 yaml 来源失败（不影响启动）：%s", e)

    # 定时任务
    from app.workers.scheduler import shutdown_scheduler, start_scheduler

    if settings.enable_scheduler:
        start_scheduler()
    else:
        logger.info("ENABLE_SCHEDULER=false，定时任务未启动")

    # ---- 推送基线 ----
    # 第一次启用推送时，把「库里已经躺着的历史通知」标记成无需推送，
    # 否则用户一开启推送就会被几百条旧通知刷屏。
    # ⚠️ 必须放在**启动时**做，不能放到推送流程里 —— 抓取和推送是同一次 job 的
    #    前后两步，放在推送里会把这一次刚抓到的新通知也一起吞掉，
    #    表现为「订阅了但一条通知都收不到」，极难排查。
    if settings.push_enabled and settings.push_silent_first_run:
        try:
            from app.services.push import PushService

            async with SessionLocal() as session:
                res = await PushService(session, settings).ensure_baseline()
            if res["done"]:
                logger.info("推送基线已建立：%d 条历史通知不会再被推送", res["marked"])
        except Exception as e:  # noqa: BLE001
            logger.warning("推送基线建立失败（不影响启动）：%s", e)

    logger.info("知了后端已就绪 · %s · 刷新计划 %s · AI=%s · 推送=%s",
                settings.env, settings.refresh_cron(),
                "开启" if settings.ai_enabled else "本地兜底",
                ",".join(settings.push_channel_list()) if settings.push_enabled else "关闭")

    yield

    shutdown_scheduler()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="知了 · 大学生校园通知聚合与 AI 提炼服务",
    lifespan=lifespan,
)

# 开发期放开跨域（Flutter web 调试用）；生产请改成具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.env == "dev" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse(
        {
            "name": settings.app_name,
            "docs": "/docs",
            "api": settings.api_prefix,
            "health": f"{settings.api_prefix}/health",
        }
    )
