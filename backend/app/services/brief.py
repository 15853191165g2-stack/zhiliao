"""今日简报：把「今天要关注什么」压成一段 30 秒能读完的话。

生成逻辑：
  - 先按截止时间与重要程度把最近的通知分桶
  - 有 AI 时让模型写 headline，没有就用模板拼
  - `speech_text` 是给 TTS 朗读用的纯口语文本（不含 emoji、不含 markdown）

注意：查询必须 selectinload(Notification.source)，
异步 Session 下访问未加载的关系会抛 MissingGreenlet（这是最容易踩的坑）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.timeutil import SHANGHAI, now_utc, to_local_dt, to_utc_naive
from app.models import Notification
from app.schemas import BriefOut, BriefSection
from app.services.ai_client import AIClient
from app.services.prompts import get_prompt_library

logger = logging.getLogger(__name__)


class BriefService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.ai = AIClient(self.settings)

    async def today(self, hours: int = 36) -> BriefOut:
        since = now_utc() - timedelta(hours=hours)
        rows = (
            (
                await self.session.execute(
                    select(Notification)
                    .options(selectinload(Notification.source))
                    .where(Notification.created_at >= since)
                    .order_by(
                        Notification.published_at.desc().nulls_last(), Notification.id.desc()
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )

        # 「即将截止」的窗口：北京时间今天 0 点 ~ 三天后 0 点（库里存的是 naive UTC）
        local_today = to_local_dt(now_utc()).replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = to_utc_naive(local_today)
        window_end = to_utc_naive(local_today + timedelta(days=4))
        assert window_start is not None and window_end is not None

        urgent: list[Notification] = []
        due_soon: list[Notification] = []
        important: list[Notification] = []
        others: list[Notification] = []

        for n in rows:
            if n.importance == "urgent":
                urgent.append(n)
            if self._deadline_within(n, window_start, window_end):
                due_soon.append(n)
            if n.importance == "important":
                important.append(n)
            if n.importance == "normal":
                others.append(n)

        counts = {
            "total": len(rows),
            "urgent": len(urgent),
            "due_soon": len(due_soon),
            "important": len(important),
            "unread": sum(1 for n in rows if not n.is_read),
        }

        data = [
            ("⚡ 紧急", urgent),
            ("⏰ 即将截止", due_soon),
            ("📌 重要", important),
            ("📰 其他更新", others),
        ]
        sections = [
            BriefSection(
                title=title,
                items=[self._line(n, with_deadline=(title == "⏰ 即将截止")) for n in bucket[:5]],
                notification_ids=[n.id for n in bucket[:5]],
            )
            for title, bucket in data
            if bucket
        ]

        headline = self._template_headline(counts)
        generated_by = "local"
        if self.ai.enabled and rows:
            try:
                headline = await self._ai_headline(counts, sections)
                generated_by = self.ai.model
            except Exception as e:  # noqa: BLE001
                logger.warning("简报 AI 生成失败，用模板：%s", e)

        date_str = to_local_dt(now_utc()).strftime("%Y-%m-%d")
        return BriefOut(
            date=date_str,
            headline=headline,
            greeting=self._greeting(),
            counts=counts,
            sections=sections,
            speech_text=self._speech_text(date_str, headline, counts, sections),
            generated_by=generated_by,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _line(n: Notification, with_deadline: bool = False) -> str:
        src = n.source.name if n.source else ""
        title = n.title[:48] + ("…" if len(n.title) > 48 else "")
        prefix = f"[{src}] " if src else ""
        extra = ""
        if with_deadline:
            deadline = (n.key_info or {}).get("deadline")
            if deadline:
                extra = f"（截止 {deadline}）"
        return f"{prefix}{title}{extra}"

    @staticmethod
    def _deadline_within(n: Notification, start_utc: datetime, end_utc: datetime) -> bool:
        """key_info.deadline_ts 存的是带 +08:00 的 ISO 字符串，先转成 naive UTC 再比。"""
        raw = (n.key_info or {}).get("deadline_ts")
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return False
        if dt.tzinfo is not None:
            dt = dt.astimezone(SHANGHAI).replace(tzinfo=None)
        # dt 现在是北京时间，再转成 naive UTC 与窗口对齐
        dt_utc = to_utc_naive(dt)
        if dt_utc is None:
            return False
        return start_utc <= dt_utc <= end_utc

    @staticmethod
    def _greeting() -> str:
        hour = to_local_dt(now_utc()).hour
        if hour < 6:
            return "这么晚还在看通知，早点休息"
        if hour < 11:
            return "早上好，今天的通知已经帮你整理好了"
        if hour < 14:
            return "中午好，快速过一遍重点"
        if hour < 18:
            return "下午好，看看有没有漏掉的事"
        return "晚上好，明天的事先安排好"

    @staticmethod
    def _template_headline(counts: dict[str, int]) -> str:
        if counts["total"] == 0:
            return "今天暂时没有新通知，安心上课"
        bits = [f"共 {counts['total']} 条新通知"]
        if counts["urgent"]:
            bits.append(f"{counts['urgent']} 条紧急")
        if counts["due_soon"]:
            bits.append(f"{counts['due_soon']} 条即将截止")
        if counts["unread"]:
            bits.append(f"{counts['unread']} 条未读")
        return "，".join(bits)

    async def _ai_headline(self, counts: dict[str, int], sections: list[BriefSection]) -> str:
        prompts = get_prompt_library()
        rendered = prompts.render(
            "brief",
            "v1",
            counts=counts,
            sections=[f"{s.title}: " + " | ".join(s.items) for s in sections],
        )
        system, user = prompts.split_system_user(rendered)
        text = await self.ai.chat(system, user, max_tokens=200)
        first = text.strip().splitlines()[0] if text.strip() else ""
        return first[:120] or self._template_headline(counts)

    @staticmethod
    def _speech_text(
        date_str: str,
        headline: str,
        counts: dict[str, int],
        sections: list[BriefSection],
    ) -> str:
        if counts["total"] == 0:
            return f"{date_str} 通知简报。今天没有新通知，可以安心安排自己的事情。"
        parts = [f"{date_str} 通知简报。{headline}。"]
        spoken = {"⚡ 紧急": "紧急事项", "⏰ 即将截止": "即将截止", "📌 重要": "重要提醒", "📰 其他更新": "其他更新"}
        for s in sections:
            title = spoken.get(s.title, s.title)
            parts.append(f"{title}：" + "；".join(s.items[:3]) + "。")
        return "".join(parts)
