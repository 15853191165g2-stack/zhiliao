"""「导入式」来源的服务层。

对应两种微信 / 学校 APP 的现实情况：

  1. **公众号文章链接导入**（微信无公开接口，只能一篇一篇导）
     用户在微信里「··· → 复制链接」，App 把链接 POST 上来。
  2. **i大工 / 学校 APP 通知导入**
     Android：NotificationListenerService 读到通知的标题+正文，本地无网也能存，
             再批量 POST 上来。
     iOS：系统不允许读别的 App 的通知，只能走「分享扩展 / 截图 OCR」，
         把文本或图片里的文字 POST 上来。

这两条路的共同点是：**没有列表可抓，数据是客户端推上来的**。
所以它们不参与定时抓取，走单独的导入入口。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import now_utc
from app.models import Notification, Source
from app.services.dedup import content_hash, make_dedup_key, make_excerpt
from app.services.ingest import IngestService

logger = logging.getLogger(__name__)

# 导入型来源在数据库里的固定标识（App 端按名字筛选）
IMPORT_SOURCES: dict[str, dict[str, str]] = {
    "微信公众号": {"adapter": "article_url", "category_hint": "其他"},
    "i大工": {"adapter": "manual_import", "category_hint": "其他"},
    "分享导入": {"adapter": "manual_import", "category_hint": "其他"},
}


async def get_or_create_import_source(session: AsyncSession, name: str) -> Source:
    """确保导入型来源存在，返回它。"""
    name = (name or "分享导入").strip()
    meta = IMPORT_SOURCES.get(name, {"adapter": "manual_import", "category_hint": "其他"})
    row = (
        await session.execute(select(Source).where(Source.name == name))
    ).scalar_one_or_none()
    if row is None:
        row = Source(
            name=name,
            school="大连理工大学",
            adapter=meta["adapter"],
            url="",
            category_hint=meta["category_hint"],
            enabled=False,  # 导入型来源不参与定时抓取
            config={"import_only": True},
        )
        session.add(row)
        await session.flush()
    return row


async def upsert_imported(
    session: AsyncSession,
    *,
    source: Source,
    title: str,
    content_text: str | None,
    url: str | None = None,
    author: str | None = None,
    published_at: datetime | None = None,
    content_html: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Notification, bool]:
    """把一条导入内容 upsert 进库。返回 (记录, 是否新建)。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")

    key = make_dedup_key(url, title, published_at)
    row = (
        await session.execute(select(Notification).where(Notification.dedup_key == key))
    ).scalar_one_or_none()
    chash = content_hash(content_text)

    if row is None:
        row = Notification(
            source_id=source.id,
            dedup_key=key,
            title=title,
            url=url,
            author=author,
            published_at=published_at,
            content_html=content_html,
            content_text=content_text,
            content_hash=chash,
            excerpt=make_excerpt(content_text),
            category=source.category_hint or "其他",
            importance="normal",
            ai_status="pending",
            is_read=False,
            is_starred=False,
        )
        session.add(row)
        await session.flush()
        return row, True

    # 已存在：只在拿到更完整的正文时更新（空正文绝不覆盖，同 ingest 的空壳保护）
    if content_text and chash and chash != row.content_hash:
        row.content_text = content_text
        row.content_html = content_html or row.content_html
        row.content_hash = chash
        row.excerpt = make_excerpt(content_text)
        row.ai_status = "pending"
        row.updated_at = now_utc()
    if extra and extra.get("import_kind"):
        row.url = row.url or url
    return row, False


async def import_article_url(
    session: AsyncSession,
    url: str,
    *,
    source_name: str = "微信公众号",
    summarize: bool = True,
    ignore_robots: bool = True,
) -> dict[str, Any]:
    """导入一篇公众号 / 任意文章链接。

    `ignore_robots` 的默认值是 **True**，这里刻意说明理由：
      - 这是**用户手动粘贴过来的单篇链接**，不是自动遍历抓取，
        语义上更接近"浏览器打开这个页面"，而不是"爬站"。
      - 但 mp.weixin.qq.com 的 robots.txt 是 `Disallow: /`，
        严格遵守就拿不到正文 —— 那样这个功能等于不存在。
      - 所以默认放行，同时把这件事**显式写进返回值**（robots_note），
        不让它变成静默行为。想严格合规就把这个参数传 false。
    """
    from app.adapters.base import FetchContext, SourceSpec
    from app.adapters.registry import adapter_for
    from app.core.config import get_settings
    from app.services.fetch import Fetcher

    settings = get_settings()
    source = await get_or_create_import_source(session, source_name)

    spec = SourceSpec(
        id=source.id,
        name=source.name,
        adapter="article_url",
        url=url,
        school=source.school or "",
        category_hint=source.category_hint or "其他",
        config={"import_only": True, "ignore_robots": bool(ignore_robots)},
    )
    async with Fetcher(settings) as fetcher:
        ctx = FetchContext(fetcher=fetcher, settings=settings)
        items = await adapter_for(spec).fetch(spec, ctx)
        overridden = fetcher.stats.get("robots_overridden", 0)

    created = 0
    ids: list[int] = []
    for item in items:
        row, is_new = await upsert_imported(
            session,
            source=source,
            title=item.title,
            content_text=item.content_text,
            url=item.url,
            author=item.author,
            published_at=item.published_at,
            content_html=item.content_html,
            extra=item.extra,
        )
        created += 1 if is_new else 0
        ids.append(row.id)

    await session.commit()

    if summarize and ids:
        service = IngestService(session, settings)
        for nid in ids:
            row = await session.get(Notification, nid)
            if row is not None and row.ai_status == "pending":
                await service.summarize_one(row)
        await session.commit()

    note = "微信不提供「按公众号拉取文章列表」的公开接口，本接口只导入单篇文章链接。"
    if overridden:
        note += (
            f" 本次有 {overridden} 次请求按「单篇手动导入」放行了 robots.txt 限制"
            "（mp.weixin.qq.com 的 robots.txt 为 Disallow: /）。"
        )

    return {
        "source": source.name,
        "imported": len(items),
        "new": created,
        "ids": ids,
        "robots_overridden": overridden,
        "note": note,
    }


async def import_texts(
    session: AsyncSession,
    entries: list[dict[str, Any]],
    *,
    source_name: str = "i大工",
    summarize: bool = True,
) -> dict[str, Any]:
    """批量导入客户端推上来的通知文本（Android 通知监听 / iOS 分享 / 截图 OCR）。

    每个 entry 形如：
        {"title": "...", "text": "...", "url": "...", "app": "i大工",
         "published_at": "2026-10-06T09:12:00+08:00"}
    """
    from app.core.timeutil import parse_dt

    source = await get_or_create_import_source(session, source_name)
    created = 0
    ids: list[int] = []
    skipped: list[str] = []

    for entry in entries:
        title = str(entry.get("title") or "").strip()
        text = str(entry.get("text") or entry.get("content") or "").strip() or None
        if not title:
            skipped.append("缺少 title")
            continue
        raw_time = entry.get("published_at") or entry.get("time")
        published = parse_dt(str(raw_time)) if raw_time else None
        row, is_new = await upsert_imported(
            session,
            source=source,
            title=title,
            content_text=text,
            url=str(entry.get("url") or "").strip() or None,
            author=str(entry.get("app") or source_name),
            published_at=published,
            extra={"import_kind": "notification_text"},
        )
        created += 1 if is_new else 0
        ids.append(row.id)

    await session.commit()

    if summarize and ids:
        from app.core.config import get_settings

        service = IngestService(session, get_settings())
        for nid in ids:
            row = await session.get(Notification, nid)
            if row is not None and row.ai_status == "pending":
                await service.summarize_one(row)
        await session.commit()

    return {
        "source": source.name,
        "received": len(entries),
        "new": created,
        "ids": ids,
        "skipped": skipped,
    }
