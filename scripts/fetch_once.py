"""知了 电脑版 · 一次性抓取全部来源并生成摘要。

用法（在 D:\\QuanZhi\\backend 下）：
    ..\\python\\python.exe -X utf8 ..\\scripts\\fetch_once.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

logging.basicConfig(level=logging.WARNING)


async def main() -> int:
    from app.db import SessionLocal
    from app.services.ingest import IngestService

    async with SessionLocal() as session:
        result = await IngestService(session).run()

    print("=" * 60)
    print("抓取结果")
    print("=" * 60)
    print(f"总条目      : {result.total_items}")
    print(f"新增        : {result.new_items}")
    print(f"更新        : {result.updated_items}")
    print(f"AI 已摘要   : {result.summarized}")
    print(f"robots 跳过 : {result.skipped_by_robots}")
    print(f"详情失败    : {result.detail_failures}")
    print(f"空壳保护    : {result.empty_kept}")
    print(f"耗时        : {result.duration_ms:.0f} ms")
    failed = [s for s in result.sources if s.get("status") != "ok"]
    print(f"失败来源    : {len(failed)} / {len(result.sources)}")
    if failed:
        # 单独说清楚：这轮没抓到的站，不证明它们没有新通知。
        # 2026-10-19 实测：代理瞬断，9 个盘锦站点全 502，汇总却是「新增 0」，
        # 看起来像「今天没有新通知」，实际是三分之一没抓到。
        print(f"!! 有 {len(failed)} 个来源本轮抓取失败（下方 !! 行）——"
              f"不代表这些站没有新通知，请稍后重跑")
    print("-" * 60)
    # 字段名必须和 ingest.py 里 result.sources 的一致：**items / new**。
    # 曾经这里读的是 item_count / new_count，两个键名对不上，
    # 于是 274 条真实抓取被打印成「24 个来源全 0 条」——
    # 和坑 57 同一类假绿：格式对、结论错，看的人会以为"今天没新通知"。
    for s in result.sources:
        flag = "OK " if s.get("status") == "ok" else "!! "
        print(
            f"  {flag}{s.get('name', '?')[:26]:28} "
            f"条目 {s.get('items', 0):>3}  新增 {s.get('new', 0):>3}"
        )

    # invariant：逐来源合计必须等于总条目。字段再错位一次就直接失败，
    # 不允许"打印全 0 但退出码 0"这种局面再出现。
    counted = sum(s.get("items", 0) for s in result.sources)
    if result.total_items != counted:
        print(f"!! 内部计数不一致：逐来源合计 {counted} ≠ 总条目 {result.total_items}")
        print("!! 大概率是 result.sources 的字段名变了，检查 ingest.py 与 fetch_once.py")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
