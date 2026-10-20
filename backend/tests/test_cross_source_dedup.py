"""跨来源重复不入库。

## 场景

同一篇通知常被多个部门站点**转发**：

    关于征集大连理工大学盘锦校区“揭榜挂帅”学生攻关榜题的通知
      → pjxqtw.dlut.edu.cn/info/1059/16048.htm   （团委）
      → xsgzb.dlut.edu.cn/info/1791/19312.htm    （学生工作部）
      → panjin.dlut.edu.cn/info/1141/54604.htm   （校区门户）

三个 URL 各不相同 → `make_dedup_key` 优先用 URL → 三个不同的 dedup_key →
各存一条，用户在信息流里连着看到三条一模一样的。

实测：2026-10-18 那次抓的 100 条里，有 4 条是这么来的。

## 修法

入库时按「标题（去掉所有空白）+ 发布日（到天）」再判一次。两条约束：

- **标题短于 10 字不参与**：「通知」「公告」「名单」这种撞车概率太高，
  误杀一条真通知比多留一条重复更难接受。
- **日期必须同一天**：每年都发的「关于放假安排的通知」是两件事，不能合并。
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.base import RawItem, SourceSpec
from app.models import Base
from app.services.ingest import IngestService

TITLE = "关于征集大连理工大学盘锦校区“揭榜挂帅”学生攻关榜题的通知"
DAY = datetime(2026, 10, 16)          # naive UTC，和库里约定一致


async def _run_async(fn):
    tmp = Path(tempfile.mkdtemp(prefix="qz_dedup_")) / "t.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp.as_posix()}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Sess() as s:
            return await fn(s)
    finally:
        await eng.dispose()


def _spec(sid: int, name: str) -> SourceSpec:
    return SourceSpec(name=name, adapter="html_list", id=sid)


def _item(url: str, title: str = TITLE, day: datetime = DAY) -> RawItem:
    return RawItem(
        title=title,
        url=url,
        published_at=day,
        content_text="这是一段足够长的正文，用来避免被判成空壳页面。" * 3,
        content_html="<p>这是一段足够长的正文，用来避免被判成空壳页面。</p>",
    )


async def _two_sources(session):
    svc = IngestService(session)
    st1 = await svc._upsert(_spec(1, "盘锦团委·通知公告"), [_item("https://a.example/1.htm")])
    await session.commit()
    st2 = await svc._upsert(
        _spec(2, "盘锦学生工作部·通知公告"), [_item("https://b.example/9.htm")]
    )
    await session.commit()
    return st1, st2


def test_cross_source_duplicate_is_skipped() -> None:
    st1, st2 = asyncio.run(_run_async(_two_sources))
    assert st1["new"] == 1
    assert st2["new"] == 0, "同标题同日期的转发不该再建一条"
    assert st2["cross_dupe"] == 1


async def _same_batch(session):
    """同一次抓取里，两个 URL 指向同一篇（门户转载 + 部门原始）。"""
    svc = IngestService(session)
    st = await svc._upsert(
        _spec(3, "盘锦校区·通知公告"),
        [_item("https://c.example/1.htm"), _item("https://c.example/2.htm")],
    )
    await session.commit()
    return st


def test_same_batch_duplicate_is_skipped() -> None:
    st = asyncio.run(_run_async(_same_batch))
    assert st["new"] == 1
    assert st["cross_dupe"] == 1


async def _short_title(session):
    svc = IngestService(session)
    st1 = await svc._upsert(_spec(4, "来源甲"), [_item("https://d.example/1.htm", "通知")])
    await session.commit()
    st2 = await svc._upsert(_spec(5, "来源乙"), [_item("https://e.example/1.htm", "通知")])
    await session.commit()
    return st1, st2


def test_short_title_still_saved() -> None:
    """「通知」这种短标题不参与跨源判重 —— 宁可留两条，也不误杀。"""
    st1, st2 = asyncio.run(_run_async(_short_title))
    assert st1["new"] == 1
    assert st2["new"] == 1, "标题太短时不做跨源判重"
    assert st2["cross_dupe"] == 0


async def _different_day(session):
    svc = IngestService(session)
    st1 = await svc._upsert(_spec(6, "来源甲"), [_item("https://f.example/1.htm")])
    await session.commit()
    st2 = await svc._upsert(
        _spec(7, "来源乙"), [_item("https://g.example/1.htm", day=datetime(2025, 10, 16))]
    )
    await session.commit()
    return st1, st2


def test_same_title_other_year_still_saved() -> None:
    """去年也有一篇同名的「放假通知」，是两件事，不能合并。"""
    st1, st2 = asyncio.run(_run_async(_different_day))
    assert st1["new"] == 1
    assert st2["new"] == 1
    assert st2["cross_dupe"] == 0
