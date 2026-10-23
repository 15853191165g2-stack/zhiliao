"""通知检索。给 AI 问答和搜索框共用。

没上向量库的理由见 docs/01-architecture.md 第 8 节：
个人量级（几千条）下，关键词检索 + 时间衰减排序完全够用，还更快更可解释。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timeutil import SHANGHAI, now_utc
from app.models import Notification, Source

logger = logging.getLogger(__name__)

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

# 泛词：出现在几乎每条通知的标题里，当关键词用等于没筛。
# 和 STOPWORDS 分开放，是因为用途不同：STOPWORDS 是"问句里的废话"，
# 这里是"检索时没有区分度的词"。
GENERIC_TERMS = {"通知", "公告", "消息", "事情", "东西", "内容", "信息"}

_DROP_TERMS = STOPWORDS | GENERIC_TERMS

# 按长度倒序交替匹配，保证「这周」先于「周」被摘掉。
_DROP_RE = re.compile(
    "|".join(sorted((re.escape(t) for t in _DROP_TERMS), key=len, reverse=True))
)

# 开放意图：问的是"最近有什么"，而不是问某个具体事物。
# 只有这类问题才允许在关键词零命中时退回最新一批（见 search_notifications）。
OPEN_INTENT = (
    "有什么", "有哪些", "哪些", "要办", "该办", "待办",
    "要注意", "值得", "重要", "最近有", "这周有", "本周有",
)

# 问句里出现这些词，说明用户要的是"可执行的事"，排序时把还没到期的截止日提前。
DEADLINE_HINTS = ("截止", "deadline", "ddl", "什么时候", "期限", "要办", "该办", "待办")


def _looks_like_noise(gram: str) -> bool:
    """含停用词/泛词的窗口，基本都是跨词拼接出来的垃圾。"""
    return any(t in gram for t in _DROP_TERMS)


def extract_keywords(question: str) -> list[str]:
    """从问题里抽关键词。

    中文没有分词器时的实用做法：

      1. **先把停用词、泛词从问句里摘掉，再切 n-gram。**
         直接在原句上切会切出「这周有什」「什么要办」这种跨词垃圾：
         整串不在 STOPWORDS 里，就躲过了过滤，最后变成注定零命中的 LIKE 条件
         ——「我这周有什么要办的事」原来是这么变成 0 条结果的。
      2. 领域词表命中 → 最高优先级，永不剔除
      3. 剩下的连续汉字段切 2-4 字窗口
      4. 丢掉被更长关键词包住的碎片
    """
    if not question:
        return []
    q = question.strip()

    # ① 领域词（钉死，不参与后续的包含关系剔除）
    pinned = [t for t in DOMAIN_TERMS if t in q]

    # ② 摘掉停用词/泛词后再切窗口
    windows: list[str] = []
    for run in _CJK_RUN.findall(_DROP_RE.sub(" ", q)):
        for size in (4, 3, 2):
            for i in range(len(run) - size + 1):
                sub = run[i : i + size]
                if sub in windows or _looks_like_noise(sub):
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


def _is_open_intent(question: str) -> bool:
    return any(w in question for w in OPEN_INTENT)


async def search_notifications(
    session: AsyncSession,
    question: str,
    *,
    days: int | None = None,
    top_k: int = 12,
) -> list[Notification]:
    """关键词召回 + 打分排序。

    两个刻意的设计，都是踩过坑之后定的：

    - **时间窗按 `published_at` 算，不按 `created_at`。**
      `created_at` 是入库时间：补抓一次历史页，七年前的通知就会以"最近"的身份混进来。
      实测（2026-10-23）「最近 7 天」按入库时间算是 115 条，最早一条发布于 2019-06-17；
      按发布时间算只有 9 条。
    - **关键词零命中时，只有"开放意图"的问题才退回最新一批。**
      问「充电桩」查不到，就该老实返回空；退回一堆不相关通知，
      模型会拿它们编出一个像样的答案——比答"没找到"更糟。
    """
    guessed = guess_days(question)
    # 问句里的时间词只会收窄窗口：调用方默认传 30 天，但用户问「这周」就该按 7 天算
    lookback = min(days, guessed) if days else guessed
    since = now_utc() - timedelta(days=lookback)
    keywords = extract_keywords(question)

    base = (
        select(Notification)
        # 必须预加载 source：异步 Session 下拿未加载的关系会抛 MissingGreenlet，
        # 而 to_out() 需要 source.name 来填卡片上的来源
        .options(selectinload(Notification.source))
        .where(Notification.published_at.isnot(None))
        .where(Notification.published_at >= since)
        .order_by(Notification.published_at.desc().nulls_last(), Notification.id.desc())
        .limit(400)
    )

    async def _fetch(with_keywords: bool) -> list[Notification]:
        stmt = base
        if with_keywords and keywords:
            clauses = []
            for kw in keywords:
                like = f"%{kw}%"
                clauses.append(Notification.title.like(like))
                clauses.append(Notification.content_text.like(like))
                clauses.append(Notification.excerpt.like(like))
            stmt = stmt.where(or_(*clauses))
        return list((await session.execute(stmt)).scalars().all())

    rows = await _fetch(with_keywords=True)

    if not rows and (not keywords or _is_open_intent(question)):
        # 零命中，但用户问的是"最近有什么" → 退回窗口内最新的一批，而不是空手而归。
        # 这不等于放宽标准：时间窗还是用户说的那个范围，只是不加关键词过滤。
        logger.info(
            "检索零命中，退回最近 %s 天最新 %s 条（关键词=%r，问句=%r）",
            lookback,
            top_k,
            keywords,
            question,
        )
        rows = await _fetch(with_keywords=False)

    # 问「要办什么」类问题时，把还没到期的截止日往前提
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    asks_deadline = any(w in question for w in DEADLINE_HINTS)

    def score(n: Notification) -> tuple:
        text = f"{n.title} {n.excerpt or ''}"
        hit = sum(1 for kw in keywords if kw in text)
        title_hit = sum(1 for kw in keywords if kw in n.title)
        deadline = str((n.key_info or {}).get("deadline") or "")[:10]
        # 已经过期的截止日不算"要办的事"，不该被顶到前面
        is_open = bool(deadline) and deadline >= today
        importance_rank = {"urgent": 3, "important": 2, "normal": 1}.get(n.importance, 0)
        deadline_bonus = (3 if asks_deadline else 1) if is_open else 0
        return (title_hit, hit + deadline_bonus, importance_rank)

    rows.sort(key=score, reverse=True)
    return rows[:top_k]


def build_context(session_rows: list[Notification], sources: dict[int, str]) -> str:
    """把召回的条目拼成给模型的上下文，带编号便于引用。"""
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    blocks: list[str] = []
    for i, n in enumerate(session_rows, start=1):
        src = sources.get(n.source_id or -1, "")
        pub = n.published_at.strftime("%Y-%m-%d") if n.published_at else "日期未知"
        info = n.key_info or {}
        meta = []
        if info.get("deadline"):
            # 过期的截止日必须标出来。不标的话模型会把「10月22日领票」当成待办推荐——
            # 日期是真的，建议是错的，最难被发现的那种错。
            expired = "（已过期，不要再推荐去办）" if str(info["deadline"])[:10] < today else ""
            meta.append(f"截止：{info['deadline']}{expired}")
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
