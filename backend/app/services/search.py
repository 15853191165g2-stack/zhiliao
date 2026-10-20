"""通知检索。给 AI 问答和搜索框共用。

没上向量库的理由见 docs/01-architecture.md 第 8 节：
个人量级（几千条）下，关键词检索 + 时间衰减排序完全够用，还更快更可解释。
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timeutil import now_utc
from app.models import Notification, Source

# 领域词表：命中即可作为强关键词
DOMAIN_TERMS = (
    "奖学金", "助学金", "励志", "国奖", "评优", "选课", "退课", "补考", "重修", "缓考",
    "四六级", "考研", "论文", "答辩", "毕业", "实习", "招聘", "宣讲", "双选会", "简历",
    "讲座", "报告会", "竞赛", "比赛", "报名", "截止", "材料", "申请表", "成绩单",
    "宿舍", "食堂", "停电", "停水", "维修", "一卡通", "校车", "体检", "开学", "注册",
    "学费", "缴费", "签证", "交换", "出国", "保研", "推免", "转专业", "学分",
)

STOPWORDS = {
    "这周", "本周", "上周", "下周", "今天", "明天", "昨天", "最近", "近期", "有没有",
    "哪些", "什么", "怎么", "如何", "需要", "要交", "提交", "告诉", "帮我", "一下",
    "我该", "我的", "是不是", "还有", "以及", "以及", "关于", "相关", "关于", "情况",
    "请问", "麻烦", "谢谢", "时间", "地点",
}

# 口语时间词 → 天数回溯
TIME_WINDOWS = {
    "今天": 1,
    "明天": 2,
    "这周": 7,
    "本周": 7,
    "下周": 14,
    "最近": 14,
    "这个月": 31,
    "本月": 31,
    "学期": 120,
}

_DATE_LIKE = re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月](\d{1,2})?")
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{2,}")


def extract_keywords(question: str) -> list[str]:
    """从问题里抽关键词。

    中文没有分词器时的实用做法：
      1. 领域词表命中 → 最高优先级，永不剔除
      2. 连续汉字段切 2-4 字窗口，提高召回
      3. 丢掉停用词，以及被更长关键词包住的碎片
    """
    if not question:
        return []
    q = question.strip()

    # ① 领域词（钉死，不参与后续的包含关系剔除）
    pinned = [t for t in DOMAIN_TERMS if t in q]

    # ② n-gram 窗口
    windows: list[str] = []
    for run in _CJK_RUN.findall(q):
        for size in (4, 3, 2):
            for i in range(len(run) - size + 1):
                sub = run[i : i + size]
                if sub in STOPWORDS or sub in windows:
                    continue
                windows.append(sub)

    # 日期
    for m in _DATE_LIKE.finditer(q):
        if m.group(0) not in windows:
            windows.append(m.group(0))

    # ③ 剔除与领域词重叠的窗口，再在两两之间去包含
    candidates = [
        w
        for w in windows
        if not any(w in p or p in w for p in pinned)
    ]
    kept: list[str] = []
    for w in sorted(candidates, key=len, reverse=True):
        if any(w != o and w in o for o in kept):
            continue
        kept.append(w)

    return (pinned + kept)[:12]


def guess_days(question: str, default: int = 30) -> int:
    for word, days in TIME_WINDOWS.items():
        if word in question:
            return days
    return default


async def search_notifications(
    session: AsyncSession,
    question: str,
    *,
    days: int | None = None,
    top_k: int = 12,
) -> list[Notification]:
    """关键词召回 + 打分排序。"""
    lookback = days or guess_days(question)
    since = now_utc() - timedelta(days=lookback)
    keywords = extract_keywords(question)

    stmt = (
        select(Notification)
        # 必须预加载 source：异步 Session 下拿未加载的关系会抛 MissingGreenlet，
        # 而 to_out() 需要 source.name 来填卡片上的来源
        .options(selectinload(Notification.source))
        .where(Notification.created_at >= since)
        .order_by(Notification.published_at.desc().nulls_last(), Notification.id.desc())
        .limit(400)
    )

    if keywords:
        clauses = []
        for kw in keywords:
            like = f"%{kw}%"
            clauses.append(Notification.title.like(like))
            clauses.append(Notification.content_text.like(like))
            clauses.append(Notification.excerpt.like(like))
        stmt = stmt.where(or_(*clauses))

    rows = list((await session.execute(stmt)).scalars().all())

    # 问「截止」类问题时，把带 deadline 的往前提
    asks_deadline = any(w in question for w in ("截止", "deadline", "什么时候", "期限"))

    def score(n: Notification) -> tuple:
        text = f"{n.title} {n.excerpt or ''}"
        hit = sum(1 for kw in keywords if kw in text)
        title_hit = sum(1 for kw in keywords if kw in n.title)
        has_deadline = 1 if (n.key_info or {}).get("deadline") else 0
        importance_rank = {"urgent": 3, "important": 2, "normal": 1}.get(n.importance, 0)
        deadline_bonus = has_deadline * (2 if asks_deadline else 1)
        return (title_hit, hit + deadline_bonus, importance_rank)

    rows.sort(key=score, reverse=True)
    return rows[:top_k]


def build_context(session_rows: list[Notification], sources: dict[int, str]) -> str:
    """把召回的条目拼成给模型的上下文，带编号便于引用。"""
    blocks: list[str] = []
    for i, n in enumerate(session_rows, start=1):
        src = sources.get(n.source_id or -1, "")
        pub = n.published_at.strftime("%Y-%m-%d") if n.published_at else "日期未知"
        info = n.key_info or {}
        meta = []
        if info.get("deadline"):
            meta.append(f"截止：{info['deadline']}")
        if info.get("audience"):
            meta.append(f"对象：{info['audience']}")
        if info.get("location"):
            meta.append(f"地点：{info['location']}")
        if info.get("materials"):
            meta.append("材料：" + "、".join(info["materials"]))
        if info.get("contact"):
            meta.append(f"联系：{info['contact']}")
        body = (n.content_text or n.excerpt or "")[:600]
        blocks.append(
            f"[{i}] {pub} {src}｜{n.title}\n"
            f"    摘要：{' / '.join(n.summary_lines or []) or '（无）'}\n"
            f"    关键信息：{'；'.join(meta) or '（无）'}\n"
            f"    正文节选：{body}"
        )
    return "\n".join(blocks) if blocks else "（没有检索到相关通知）"


async def source_name_map(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(Source.id, Source.name))).all()
    return {r[0]: r[1] for r in rows}
