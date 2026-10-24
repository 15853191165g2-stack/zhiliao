"""FastAPI 应用入口。

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
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

    # ---- 生效模型配置 ----
    # 库里激活了自定义模型就用它，否则回落 .env。必须在这里加载一次：
    # llm_config 是进程级缓存，不预热的话第一次 get_active 会直接回落 .env，
    # 表现为「明明在界面上启用了自定义模型，重启后又跑回 GLM 了」。
    from app.services.llm_config import reload_active

    active_llm = await reload_active(settings)
    logger.info("生效模型：%s（来源 %s，地址 %s）",
                active_llm.label, active_llm.source, active_llm.endpoint or "未配置")

    # 定时任务
    from app.workers.scheduler import shutdown_scheduler, start_scheduler

    if settings.enable_scheduler:
        start_scheduler()
    else:
        logger.info("ENABLE_SCHEDULER=false，定时任务未启动")

    # ---- 启动补抓 ----
    # 调度器是进程内的，程序关掉期间错过的时间点不会回补。
    # 这里在后台对一下「上次抓取是什么时候」，陈旧就补一轮 ——
    # 必须 create_task，不能 await：抓一轮要 4 分钟左右，等它跑完再启动
    # 会让客户端以为后端挂了（健康检查一直不通）。
    catchup_task = None
    if settings.enable_scheduler and settings.refresh_catchup_on_start:
        from app.workers.scheduler import catchup_if_stale

        catchup_task = asyncio.create_task(catchup_if_stale())

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
                "开启" if active_llm.enabled else "本地兜底",
                ",".join(settings.push_channel_list()) if settings.push_enabled else "关闭")

    yield

    # 正在补抓就先取消（进程要退出了，抓完也存不进去）
    if catchup_task is not None and not catchup_task.done():
        catchup_task.cancel()
        try:
            await catchup_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

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
