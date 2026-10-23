"""清理历史遗留的跨部门重复通知。

用法（在 D:\\QuanZhi\\backend 下）：
    ..\\python\\python.exe -X utf8 ..\\scripts\\dedup_cleanup.py            # 只出清单，不写库
    ..\\python\\python.exe -X utf8 ..\\scripts\\dedup_cleanup.py --apply    # 真删

## 为什么需要这个脚本

`services/dedup.py` 的「标题去空白 + 发布日到天」判重是**入库时**生效的，
所以在它加进来之前入库的重复条目不会被自动清掉。实测（2026-10-23）库里有 3 组：

    2026-10-15  关于征集…盘锦校区"揭榜挂帅"…   id 169 / 192 / 224（三个部门转发）
    2026-08-09  2026级新生医疗保险投保指南       id 105 / 116
    2025-07-19  大连理工大学新生国家助学贷款…    id 114 / 124

## 判定条件（三条同时满足才合并）

1. 标题去掉所有空白后相同，且标题长度 ≥ 10 字
   （短标题撞车概率太高，误杀一条真通知比多留一条重复更难接受）
2. 发布日同一天
3. **content_hash 完全相同** —— 这条是核心。只按标题+日期判重会把
   「本科生版 / 研究生版」这种同名不同文的通知合并掉，用户就丢了一份材料。
   实测那 3 组的 content_hash 确实逐一相同，才敢动手。

## 保留哪一条

正文最长的那条（信息最全）；一样长取 id 小的（先入库的，推送日志更早）。
用户状态（已读 / 星标 / 置顶）按「或」合并到保留条上——删掉一条已读的，
不能让用户觉得"我明明看过它又变未读了"。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

MIN_TITLE_LEN = 10


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def _day(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


async def main(apply: bool) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Notification

    async with SessionLocal() as session:
        rows = list((await session.execute(select(Notification))).scalars().all())

        groups: dict[tuple[str, str], list[Notification]] = {}
        for n in rows:
            key = (_norm_title(n.title), _day(n.published_at))
            groups.setdefault(key, []).append(n)

        plan = []
        rejected = []
        for (title, day), items in groups.items():
            if len(items) < 2:
                continue
            if len(title) < MIN_TITLE_LEN:
                rejected.append((title, day, "标题过短，不参与"))
                continue
            hashes = {n.content_hash or "" for n in items}
            if len(hashes) > 1:
                # 同名不同文：本科/研究生版、逐年重发的通知都可能撞标题，必须放过
                rejected.append((title, day, f"正文不同（{len(hashes)} 个 hash）"))
                continue

            keeper = max(items, key=lambda n: (len(n.content_text or ""), -n.id))
            victims = [n for n in items if n.id != keeper.id]
            plan.append((keeper, victims))

        print("=" * 74)
        print(f"{'只出清单（加 --apply 才会真删）' if not apply else '!! 即将写库'}")
        print("=" * 74)
        print(f"通知总数 {len(rows)}，重复组 {len(plan)} 组，将删除 {sum(len(v) for _, v in plan)} 条\n")

        for keeper, victims in plan:
            print(f"[保留] id {keeper.id}  {keeper.title[:52]}")
            print(
                f"        {_day(keeper.published_at)}  正文 {len(keeper.content_text or '')} 字  "
                f"来源 id {keeper.source_id}"
            )
            for v in victims:
                print(
                    f"[删除] id {v.id}  {_day(v.published_at)}  正文 "
                    f"{len(v.content_text or '')} 字  来源 id {v.source_id}  "
                    f"已读={int(v.is_read)} 星标={int(v.is_starred)} 置顶={int(v.is_pinned)}"
                )
            print()

        if rejected:
            print("-- 标题+同日相同但**未合并**的（正文不同，必须保留）--")
            for title, day, why in rejected:
                print(f"   {day}  {title[:48]}  ← {why}")
            print()

        if not plan:
            print("没有需要清理的重复。")
            return 0

        manifest = [
            {
                "title": k.title,
                "day": _day(k.published_at),
                "keep": k.id,
                "delete": [v.id for v in vs],
            }
            for k, vs in plan
        ]

        if not apply:
            print("清单已生成，未改动数据库。确认后加 --apply 执行。")
            print(f"（本次计划：{json.dumps(manifest, ensure_ascii=False)}）")
            return 0

        # 写库前先把库拷一份：删错了还能退回去
        from app.core.config import get_settings

        db_url = get_settings().database_url
        if db_url.startswith("sqlite"):
            db_path = Path(db_url.split("///")[-1].split("?")[0])
            if not db_path.is_absolute():
                db_path = BACKEND / db_path
            if db_path.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = db_path.with_name(f"{db_path.name}.bak-dedup-{stamp}")
                shutil.copy2(db_path, backup)
                print(f"已备份数据库 → {backup}")

        removed = 0
        for keeper, victims in plan:
            # 用户状态按「或」合并：不能因为删掉一条已读的，让用户以为没看过
            keeper.is_read = keeper.is_read or any(v.is_read for v in victims)
            keeper.is_starred = keeper.is_starred or any(v.is_starred for v in victims)
            keeper.is_pinned = keeper.is_pinned or any(v.is_pinned for v in victims)
            # 标签取并集：各站的 AI 摘要给的关键词不完全一样，并起来信息更全
            merged_tags = list(dict.fromkeys([*(keeper.tags or [])] + [t for v in victims for t in (v.tags or [])]))
            if merged_tags:
                keeper.tags = merged_tags
            for v in victims:
                await session.delete(v)
                removed += 1

        await session.commit()
        print(f"\n完成：删除 {removed} 条，保留 {len(plan)} 条。")
        if cleaned := [m for m in manifest]:
            print(f"清单：{json.dumps(cleaned, ensure_ascii=False)}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
