"""真实抓取体检：把每个来源跑一遍，逐字段报命中数。

这一步的意义：**用真实页面验证配置，而不是等入库之后再猜哪里不对。**
输出里每个字段都给出「取到几条 / 共几条」的具体数字，缺失就一眼看得出来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.base import FetchContext, SourceSpec  # noqa: E402
from app.adapters.registry import adapter_for, load_builtin  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services.fetch import Fetcher  # noqa: E402
from app.services.source_config import load_yaml_sources  # noqa: E402


async def check(entry: dict, sem: asyncio.Semaphore, only_pages: bool):
    spec = SourceSpec.from_dict({k: v for k, v in entry.items() if not k.startswith("_")})
    settings = get_settings()
    async with sem:
        async with Fetcher(settings) as fetcher:
            ctx = FetchContext(fetcher=fetcher, settings=settings)
            try:
                items = await adapter_for(spec).fetch(spec, ctx)
            except Exception as e:  # noqa: BLE001
                return spec, None, f"{type(e).__name__}: {e}"
            requests = fetcher.stats["requests"]
    return spec, items, f"requests={requests}"


def report(spec: SourceSpec, items, note: str) -> tuple[int, int, int]:
    print("=" * 78)
    print(f"[{spec.name}]  {spec.url}")
    if items is None:
        print(f"   ❌ 抓取失败 -> {note}")
        return 0, 0, 0

    n = len(items)
    with_date = sum(1 for i in items if i.published_at)
    with_url = sum(1 for i in items if i.url)
    with_text = sum(1 for i in items if i.content_text)
    with_empty = sum(1 for i in items if i.extra.get("content_empty"))
    failed = sum(1 for i in items if i.extra.get("detail_failed"))
    print(f"   条目 {n} 条 | {note}")
    print(f"   有链接 {with_url}/{n}   有日期 {with_date}/{n}   有正文 {with_text}/{n}"
          f"   正文为空壳 {with_empty}   详情失败 {failed}")
    for it in items[:3]:
        d = it.published_at.strftime("%Y-%m-%d") if it.published_at else "—"
        tl = len(it.content_text or "")
        print(f"     · {d}  {it.title[:44]:46s} 正文{tl:5d}字")
        print(f"       {it.url}")
    if with_date < n:
        print("   ⚠️ 有条目没解析出日期：")
        for it in items:
            if not it.published_at:
                print(f"      - {it.title[:50]}")
    return n, with_date, with_text


async def main():
    load_builtin()
    entries = [
        e
        for e in load_yaml_sources()
        if e.get("enabled") and str(e.get("school") or "") == "大连理工大学"
    ]
    if not entries:
        print("没有启用的大工来源")
        return

    sem = asyncio.Semaphore(4)
    print(f"开始体检 {len(entries)} 个来源…\n")
    results = await asyncio.gather(*(check(e, sem, True) for e in entries))

    total = with_date = with_text = 0
    for spec, items, note in results:
        n, d, t = report(spec, items, note)
        total += n
        with_date += d
        with_text += t

    print("=" * 78)
    print(f"合计：{total} 条；有日期 {with_date}/{total}；有正文 {with_text}/{total}")


if __name__ == "__main__":
    asyncio.run(main())
