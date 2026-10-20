"""大工来源全链路实跑：同步 yaml → 抓取 → 去重入库 → AI 摘要 → 打印一条完整输出。

用法：
    python scripts/e2e_dlut.py            # 走全部已启用的大工来源
    python scripts/e2e_dlut.py --reset    # 先清空数据库再跑（干净验收）
    python scripts/e2e_dlut.py --no-summarize
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import FetchLog, Notification, Source  # noqa: E402
from app.schemas import IMPORTANCE_LABELS  # noqa: E402
from app.services.ingest import IngestService  # noqa: E402
from app.services.source_config import sync_yaml_to_db  # noqa: E402


async def reset(session) -> None:
    await session.execute(delete(Notification))
    await session.execute(delete(FetchLog))
    await session.execute(delete(Source))
    await session.commit()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空数据库再跑")
    ap.add_argument("--no-summarize", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    await init_db()
    # 建表后立刻释放连接，避免与后续会话抢占 SQLite 写锁
    await engine.dispose()

    async with SessionLocal() as session:
        if args.reset:
            await reset(session)
            print("[1/4] 数据库已清空")

        sync = await sync_yaml_to_db(session)
        enabled = (
            await session.execute(select(func.count(Source.id)).where(Source.enabled.is_(True)))
        ).scalar_one()
        print(f"[2/4] 来源同步：新增 {sync['created']}，更新 {sync['updated']}，"
              f"yaml 共 {sync['total']} 条；当前启用 {enabled} 个")

        service = IngestService(session, settings)
        result = await service.run(do_summarize=not args.no_summarize)

        print(f"[3/4] 抓取完成：{result.total_items} 条 | 新增 {result.new_items}，"
              f"更新 {result.updated_items}，摘要 {result.summarized} 条 | "
              f"{result.duration_ms}ms")
        print(f"      合规：robots 拦截 {result.skipped_by_robots} 次；"
              f"空壳保护命中 {result.empty_kept} 次；详情失败 {result.detail_failures} 条")
        print("      分来源：")
        for s in result.sources:
            if s.get("status") != "ok":
                print(f"        ❌ {s['name']}: {s.get('error')}")
                continue
            extra = ""
            if s.get("empty_kept"):
                extra += f" 空壳保护{s['empty_kept']}"
            if s.get("detail_failures"):
                extra += f" 详情失败{s['detail_failures']}"
            print(f"        · {s['name']:26s} {s['items']:3d} 条  新增 {s['new']:3d}{extra}")

        total = (
            await session.execute(select(func.count(Notification.id)))
        ).scalar_one()
        print(f"[4/4] 库内通知共 {total} 条")

        # ---- 打印一条完整 AI 输出，作为验收样本 ----
        # 选样本：优先「像通知的」——解析出了截止时间的行（可执行、有结构），
        # 备选才退回最长正文。避免拿《新生家长指南》这种长篇散文当门面。
        candidates = (
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.ai_status == "done")
                    .where(Notification.content_text.isnot(None))
                    .order_by(func.length(Notification.content_text).desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        row = next(
            (r for r in candidates if (r.key_info or {}).get("deadline")),
            candidates[0] if candidates else None,
        )
        if row is None:
            row = (
                await session.execute(
                    select(Notification).order_by(Notification.id.desc()).limit(1)
                )
            ).scalar_one_or_none()

        if row is not None:
            src = await session.get(Source, row.source_id) if row.source_id else None
            payload = {
                "id": row.id,
                "title": row.title,
                "source": src.name if src else None,
                "url": row.url,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "category": row.category,
                "importance": row.importance,
                "importance_label": IMPORTANCE_LABELS.get(row.importance, "一般"),
                "summary": row.summary_lines or [],
                "key_info": row.key_info or {},
                "tags": row.tags or [],
                "ai_status": row.ai_status,
                "ai_model": row.ai_model,
                "content_chars": len(row.content_text or ""),
            }
            print("\n" + "=" * 74)
            print("验收样本：一条通知的完整 AI 输出")
            print("=" * 74)
            print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
