"""重新生成已入库通知的摘要（不重新抓取）。

用途：改了 `summarize.py` 的规则、或改了 `app/prompts/summarize.v1.md`
之后，不必重新跑一遍网络抓取，直接对库里的行重跑摘要即可。

用法：
    python scripts/resummarize.py                 # 重跑全部有正文的通知
    python scripts/resummarize.py --limit 50      # 只重跑最新的 50 条
    python scripts/resummarize.py --status pending  # 只补还没摘要的（不动已完成的）
    python scripts/resummarize.py --model local-fallback  # 只重跑上次限流降级的
    python scripts/resummarize.py --ids 12,34,56   # 只重跑指定 id（改了规则修一小批）
    python scripts/resummarize.py --sample        # 跑完打印一条完整输出（验收样本）

注意 `--status all`（默认）会把**已经 done 的行也打回 pending 重跑**，
只为「改了规则/prompt 需要全量重算」时用；日常补摘要一律带 `--status pending`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, update  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Notification, Source  # noqa: E402
from app.schemas import IMPORTANCE_LABELS  # noqa: E402
from app.services.ingest import IngestService  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只重跑最新 N 条（0 = 全部）")
    ap.add_argument(
        "--status",
        default="all",
        choices=["all", "pending", "done", "failed"],
        help="按 ai_status 筛：all=全部有正文的行（默认，会把 done 也重算）；"
        "pending=只补还没摘要的；failed=只重跑抛异常的",
    )
    ap.add_argument(
        "--model",
        default="",
        help="按 ai_model 筛（如 local-fallback=上次限流降级的、local-no-content=没抓到正文的）。"
        "注意 local-fallback 是**模型**不是状态，写成 --status local-fallback 会一事无成",
    )
    ap.add_argument(
        "--ids",
        default="",
        help="只重跑指定 id（逗号/空格分隔），优先于 --status 和 --limit",
    )
    ap.add_argument("--sample", action="store_true", help="跑完打印一条完整输出")
    args = ap.parse_args()

    settings = get_settings()
    await init_db()
    await engine.dispose()

    async with SessionLocal() as session:
        # 先把「本次该跑的 id」定下来，再统一打回 pending。
        # all = 老行为（全部有正文的行）；其余 = 只有该状态的行，已完成的保持原样。
        # 定下 id 的好处：不会顺手把「别的状态的积压」一起跑掉（--status local-fallback
        # 时尤其重要，否则会把几百条 pending 也带上）。
        if args.ids:
            # 显式指定 id：用于「只修某几条」的场景（改了规则要重算一小批）。
            target_ids = [int(x) for x in re.split(r"[,\s]+", args.ids) if x.strip()]
            if not target_ids:
                print("--ids 里没有有效的 id")
                return
        else:
            scope = [Notification.content_text.isnot(None)]
            if args.status != "all":
                scope.append(Notification.ai_status == args.status)
            if args.model:
                scope.append(Notification.ai_model == args.model)

            id_stmt = select(Notification.id).where(*scope).order_by(
                Notification.published_at.desc().nulls_last(),
                Notification.id.desc(),
            )
            if args.limit > 0:
                id_stmt = id_stmt.limit(args.limit)
            target_ids = list((await session.execute(id_stmt)).scalars().all())

        if not target_ids:
            print(f"没有需要重跑的记录（status={args.status}）")
            return

        await session.execute(
            update(Notification)
            .where(Notification.id.in_(target_ids))
            .values(ai_status="pending")
        )
        await session.commit()

        pending = len(target_ids)
        if args.ids:
            print(f"本次范围：指定 {pending} 个 id")
        else:
            print(f"本次范围：status={args.status}"
                  f"{'，model=' + args.model if args.model else ''}"
                  f"{'，limit=' + str(args.limit) if args.limit else ''}")
        print(f"待跑摘要：{pending} 条 | 摘要引擎："
              f"{'GLM(' + settings.ai_model + ')' if settings.ai_enabled else 'local 本地规则'}")

        service = IngestService(session, settings)
        # 分批跑：每批 10 条由 summarize_pending 内部提交一次。
        # 免费档 GLM 限流严重，一批几百条中途被打断就全丢，分批才能断点续跑。
        done = 0
        started = time.perf_counter()
        while done < pending:
            batch = target_ids[done: done + 10]
            n = await service.summarize_pending(limit=len(batch), ids=batch)
            if n == 0:
                break
            done += n
            elapsed = time.perf_counter() - started
            eta = (elapsed / done) * (pending - done) if done else 0
            print(
                f"  进度 {done}/{pending} ({done * 100 // pending}%)  "
                f"已用 {elapsed / 60:.1f} min  预计还需 {eta / 60:.1f} min",
                flush=True,
            )
        print(f"完成：{done} 条  总耗时 {(time.perf_counter() - started) / 60:.1f} min",
              flush=True)

        if not args.sample:
            return

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
            print("没有可展示的样本")
            return

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
        print("验收样本：一条通知的完整摘要输出")
        print("=" * 74)
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
