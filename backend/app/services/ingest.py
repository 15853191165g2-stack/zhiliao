"""抓取编排：来源 → 适配器 → 去重 → 入库 → 触发 AI 摘要。

这是后端的「主流程」，定时任务和 App 下拉刷新调的都是它。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError, FetchContext, RawItem, SourceSpec
from app.adapters.registry import adapter_for
from app.core.config import Settings, get_settings
from app.core.timeutil import now_utc
from app.models import FetchLog, Notification, Source
from app.schemas import IMPORTANCE_LABELS, IngestResult
from app.services.dedup import (
    content_hash,
    is_shell_content,
    make_dedup_key,
    make_excerpt,
    structural_tags,
)
from app.services.fetch import Fetcher, RobotsDenied
from app.services.summarize import SummarizeService

logger = logging.getLogger(__name__)


class IngestService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ #
    async def run(
        self,
        *,
        source_ids: Iterable[int] | None = None,
        do_summarize: bool | None = None,
    ) -> IngestResult:
        started = time.perf_counter()
        result = IngestResult()

        stmt = select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
        if source_ids is not None:
            ids = list(source_ids)
            stmt = select(Source).where(Source.id.in_(ids)).order_by(Source.id)
        sources = list((await self.session.execute(stmt)).scalars().all())
        if not sources:
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result

        specs = [SourceSpec.from_orm(s) for s in sources]
        source_by_id = {s.id: s for s in sources}

        # ---- 1) 并发抓取（抓取并发，写库串行，避免 Session 打架）----
        fetched: list[tuple[SourceSpec, list[RawItem] | None, str | None]] = []
        async with Fetcher(self.settings) as fetcher:
            ctx = FetchContext(fetcher=fetcher, settings=self.settings)
            sem = asyncio.Semaphore(max(1, self.settings.fetch_concurrency))

            async def one(spec: SourceSpec):
                async with sem:
                    try:
                        adapter = adapter_for(spec)
                        items = await adapter.fetch(spec, ctx)
                        return spec, items, None
                    except RobotsDenied as e:
                        return spec, None, f"ROBOTS_DENIED: {e}"
                    except (AdapterError, Exception) as e:  # noqa: BLE001
                        logger.warning("抓取失败 [%s]：%s", spec.name, e)
                        return spec, None, f"{type(e).__name__}: {e}"

            fetched = list(await asyncio.gather(*(one(s) for s in specs)))
            robots_blocked = fetcher.stats["blocked_by_robots"]

        # ---- 2) 串行入库 ----
        for spec, items, error in fetched:
            row = source_by_id.get(spec.id) if spec.id else None
            log = FetchLog(
                source_id=spec.id,
                source_name=spec.name,
                started_at=now_utc(),
            )
            self.session.add(log)

            if error:
                log.status = "blocked" if error.startswith("ROBOTS_DENIED") else "error"
                log.error = error
                log.finished_at = now_utc()
                if row:
                    row.last_status = log.status
                    row.last_error = error
                    row.last_fetched_at = now_utc()
                result.sources.append({"name": spec.name, "status": log.status, "error": error})
                continue

            items = items or []
            result.total_items += len(items)
            stats = await self._upsert(spec, items)
            result.new_items += stats["new"]
            result.updated_items += stats["updated"]
            result.empty_kept += stats["empty_kept"]
            result.detail_failures += stats["detail_failures"]

            log.status = "ok"
            log.item_count = len(items)
            log.new_count = stats["new"]
            log.finished_at = now_utc()
            if row:
                row.last_status = "ok"
                row.last_error = None
                row.last_fetched_at = now_utc()
                row.item_count = len(items)
            result.sources.append(
                {
                    "name": spec.name,
                    "status": "ok",
                    "items": len(items),
                    "new": stats["new"],
                    "empty_kept": stats["empty_kept"],
                    "detail_failures": stats["detail_failures"],
                }
            )

        await self.session.commit()

        # ---- 3) AI 摘要 ----
        should = self.settings.auto_summarize if do_summarize is None else do_summarize
        if should:
            result.summarized = await self.summarize_pending(
                limit=self.settings.summarize_batch_limit
            )

        if robots_blocked:
            # 用 Fetcher 的**请求级**计数做准：它同时覆盖「信息源列表页被拒」
            # 和「详情页被拒」两种情况。早先只统计列表页被拒，会出现
            # 「日志打了 17 条 robots 跳过、汇总却写 0 次」的自相矛盾。
            result.skipped_by_robots = robots_blocked
            logger.info("本次有 %d 个请求因 robots.txt 被跳过", robots_blocked)

        result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    @staticmethod
    def _title_day_key(title: str | None, published_at: datetime | None) -> str | None:
        """跨来源判重用的键：标题（去掉所有空白）+ 发布日（到天）。

        返回 None 表示**不参与**跨来源判重：
          · 标题为空或短于 10 字 —— 「通知」「公告」「名单」这种标题撞车概率太高，
            误杀一条真通知比多留一条重复更难接受。
        """
        t = re.sub(r"[\s\u3000]+", "", title or "")
        if len(t) < 10:
            return None
        day = published_at.strftime("%Y-%m-%d") if published_at else ""
        return f"{t}|{day}"

    # ------------------------------------------------------------------ #
    async def _upsert(self, spec: SourceSpec, items: list[RawItem]) -> dict[str, int]:
        """写入或更新。

        两道防线，针对的是同一个事故：**一次网络抖动把历史正文清空**。
          防线1（适配器层）：详情页抓失败时保留列表已知字段，并打上
                             `detail_failed` 标记，不静默吞掉。
          防线2（本层）：即使上游确实给回来一个空壳，也**不覆盖**库里已有的正文，
                        并且不把 content_hash 改掉（否则会被误判成「原文被修改」）。
        """
        stats = {
            "new": 0,
            "updated": 0,
            "empty_kept": 0,
            "detail_failures": 0,
            "cross_dupe": 0,     # 因「跨来源同标题同日期」跳过的条数
        }
        if not items:
            return stats

        stats["detail_failures"] = sum(1 for it in items if it.extra.get("detail_failed"))

        prepared: list[tuple[RawItem, str, str, bool]] = []
        for item in items:
            key = make_dedup_key(item.url, item.title, item.published_at)
            shell = is_shell_content(item.content_html, item.content_text)
            chash = "" if shell else content_hash(item.content_text or item.content_html)
            prepared.append((item, key, chash, shell))

        keys = [k for _, k, _, _ in prepared]
        existing_rows = (
            (await self.session.execute(select(Notification).where(Notification.dedup_key.in_(keys))))
            .scalars()
            .all()
        )
        by_key = {r.dedup_key: r for r in existing_rows}

        # ---- 跨来源判重 ----
        # 同一篇通知经常被多个部门站点**转发**：「关于征集…揭榜挂帅…攻关榜题的通知」
        # 同时出现在团委、学生工作部、校区门户三个站，URL 各不相同 →
        # dedup_key 不同 → 各存一条 → 用户在信息流里连着看到三条一模一样的
        # （实测：100 条新通知里有 4 条是这么来的）。
        #
        # 所以再按「标题（去空白）+ 发布日（到天）」判一次：
        #   · 标题短于 10 字不参与 —— 「通知」「公告」这种撞车就误杀了；
        #   · 日期必须同一天 —— 同名但跨年的「放假通知」是两件事。
        seen_td: set[str] = set()
        titles_in_batch = {it.title for it, _, _, _ in prepared if it.title}
        if titles_in_batch:
            same_title_rows = (
                await self.session.execute(
                    select(Notification.title, Notification.published_at).where(
                        Notification.title.in_(titles_in_batch)
                    )
                )
            ).all()
            for t, d in same_title_rows:
                k = self._title_day_key(t, d)
                if k:
                    seen_td.add(k)

        for item, key, chash, shell in prepared:
            row = by_key.get(key)
            category = item.category or spec.category_hint or "其他"
            if row is None:
                # 跨来源转发：库里/本批里已经有同标题同日期的了，就不再建一条
                td = self._title_day_key(item.title, item.published_at)
                if td is not None and td in seen_td:
                    stats["cross_dupe"] += 1
                    logger.debug(
                        "跳过跨来源重复：[%s] %s", spec.name, item.title[:40]
                    )
                    continue
                row = Notification(
                    source_id=spec.id,
                    dedup_key=key,
                    title=item.title,
                    url=item.url,
                    author=item.author,
                    published_at=item.published_at,
                    content_html=item.content_html,
                    content_text=item.content_text,
                    content_hash=chash,
                    excerpt=make_excerpt(item.content_text or item.extra.get("excerpt")),
                    category=category,
                    importance="normal",
                    tags=structural_tags(item.content_html, item.content_text),
                    ai_status="pending",
                    is_read=False,
                    is_starred=False,
                )
                self.session.add(row)
                by_key[key] = row
                if td is not None:
                    seen_td.add(td)      # 同一次抓取里也不许再建一条一样的
                stats["new"] += 1
                continue

            # ---- 防线2：空壳不覆盖已有正文 ----
            if shell and (row.content_text or row.content_html):
                stats["empty_kept"] += 1
                if not row.url and item.url:
                    row.url = item.url
                continue

            # 已存在：正文变了才更新，并重新排队做摘要
            if chash and chash != row.content_hash:
                row.content_html = item.content_html or row.content_html
                row.content_text = item.content_text or row.content_text
                row.content_hash = chash
                row.title = item.title or row.title
                row.excerpt = make_excerpt(row.content_text)
                row.ai_status = "pending"
                row.updated_at = now_utc()
                stats["updated"] += 1
                continue

            # 内容没变，只补齐空缺字段
            changed = False
            if not row.url and item.url:
                row.url = item.url
                changed = True
            if not row.published_at and item.published_at:
                row.published_at = item.published_at
                changed = True
            if not row.content_text and item.content_text:
                row.content_text = item.content_text
                row.content_hash = chash
                row.excerpt = make_excerpt(item.content_text)
                row.ai_status = "pending"
                changed = True
            if changed:
                row.updated_at = now_utc()
                stats["updated"] += 1

        await self.session.flush()
        if stats["cross_dupe"]:
            logger.info(
                "[%s] 跳过 %d 条跨来源重复（同标题同日期的转发）",
                spec.name, stats["cross_dupe"],
            )
        return stats

    # ------------------------------------------------------------------ #
    async def summarize_pending(
        self, limit: int = 20, ids: Iterable[int] | None = None
    ) -> int:
        """把待处理的通知送去生成摘要。

        `ids` 用于把范围精确限定到一批行（脚本补摘要时用）。不给就是所有 pending ——
        定时任务走这条路径，会把积压一起消化掉，这是期望行为。
        """
        stmt = select(Notification).where(Notification.ai_status == "pending")
        if ids is not None:
            id_list = list(ids)
            if not id_list:
                return 0
            stmt = stmt.where(Notification.id.in_(id_list))
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(
                        Notification.published_at.desc().nulls_last(), Notification.id.desc()
                    ).limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0

        service = SummarizeService(self.settings)
        done = 0
        for row in rows:
            await self.summarize_one(row, service)
            done += 1
        await self.session.commit()
        return done

    async def summarize_one(
        self, row: Notification, service: SummarizeService | None = None
    ) -> Notification:
        """对单条通知跑摘要。失败也保留原文，只把 ai_status 标成 failed。"""
        svc = service or SummarizeService(self.settings)
        source_name = ""
        school = ""
        if row.source_id:
            src = await self.session.get(Source, row.source_id)
            if src:
                source_name = src.name
                school = src.school or ""

        try:
            res = await svc.summarize(
                title=row.title,
                content=row.content_text or row.excerpt,
                source_name=source_name,
                school=school,
                published_at=row.published_at.strftime("%Y-%m-%d %H:%M") if row.published_at else "",
                category_hint=row.category or "其他",
            )
            data = res.summary.model_dump(mode="json")
            row.summary_lines = data.get("summary_lines") or []
            row.key_info = data.get("key_info") or {}
            row.importance = data.get("importance") or "normal"
            if data.get("category"):
                row.category = data["category"]
            # AI 给的标签 + 结构标记（「图片通知」「正文为空」）合并，
            # 后者是抓取事实，不能被 AI 的输出覆盖掉
            merged: list[str] = []
            for tag in structural_tags(row.content_html, row.content_text) + list(
                data.get("tags") or []
            ):
                tag = str(tag).strip()
                if tag and tag not in merged:
                    merged.append(tag)
            row.tags = merged[:8]
            row.ai_status = "done"
            row.ai_model = res.model
            row.ai_error = res.error
            row.ai_updated_at = now_utc()
        except Exception as e:  # noqa: BLE001
            logger.exception("摘要失败 notification#%s", row.id)
            row.ai_status = "failed"
            row.ai_error = str(e)[:500]
            row.ai_updated_at = now_utc()

        await self.session.flush()
        return row

    # ------------------------------------------------------------------ #
    @staticmethod
    def importance_label(value: str) -> str:
        return IMPORTANCE_LABELS.get(value, "一般")


async def ingest_once(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    source_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """给定时任务/脚本用的便捷入口。"""
    service = IngestService(session, settings)
    result = await service.run(source_ids=source_ids)
    return result.model_dump()


def make_fetch_log_summary(log: FetchLog) -> dict[str, Any]:
    return {
        "source": log.source_name,
        "status": log.status,
        "new": log.new_count,
        "items": log.item_count,
        "at": log.started_at.isoformat() if isinstance(log.started_at, datetime) else None,
    }
