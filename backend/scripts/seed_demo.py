"""初始化演示数据并跑一次完整抓取 + AI 摘要。

    python scripts/seed_demo.py

跑完就可以启动服务看效果了：
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Notification, Source  # noqa: E402
from app.services.ingest import IngestService  # noqa: E402
from app.services.source_config import sync_yaml_to_db  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


async def main() -> None:
    settings = get_settings()
    print("=" * 66)
    print("知了 · 演示数据初始化")
    print("=" * 66)
    print(f"数据库      : {settings.database_url}")
    print(f"AI 模式     : {'智谱 ' + settings.ai_model if settings.ai_enabled else '本地规则兜底（未配置 AI_API_KEY）'}")
    print(f"刷新计划    : {settings.refresh_cron()} ({settings.timezone})")
    print("-" * 66)

    await init_db()

    async with SessionLocal() as session:
        stats = await sync_yaml_to_db(session)
        print(f"[1/3] 来源同步：新增 {stats['created']}，更新 {stats['updated']}")

        result = await IngestService(session).run()
        print(
            f"[2/3] 抓取完成：共 {result.total_items} 条，"
            f"新增 {result.new_items}，更新 {result.updated_items}，"
            f"AI 摘要 {result.summarized} 条，耗时 {result.duration_ms:.0f}ms"
        )
        for s in result.sources:
            flag = "OK " if s.get("status") == "ok" else "!! "
            print(f"      {flag}{s.get('name')}  {s}")

        total = int(
            (await session.execute(select(func.count()).select_from(Notification))).scalar() or 0
        )
        sources = int(
            (await session.execute(select(func.count()).select_from(Source))).scalar() or 0
        )
        print(f"[3/3] 入库总计：来源 {sources} 个，通知 {total} 条")

    await engine.dispose()
    print("-" * 66)
    print("完成。启动服务：uvicorn app.main:app --reload --port 8000")
    print("接口文档：http://127.0.0.1:8000/docs")


if __name__ == "__main__":
    asyncio.run(main())
